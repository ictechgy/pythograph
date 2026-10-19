"""SQL 텍스트에서 관계(테이블·뷰) 이름을 읽는 어휘 기반 추출기.

tsograph `src/schema/sql-relations.ts`(dartograph `sql_relations.dart`·kartograph `SqlRelations.kt`·cartograph
`SqlRelations.swift`의 포트)를 한 규칙씩 옮겼다. 생산자마다 같은 SQL을 다르게 읽으면 isthmus persistence 조인
결과가 생산자 언어에 따라 달라지므로 알고리즘·게이트·미해석 계수 규칙을 가족과 같게 유지한다. 가족 공유
벡터는 `tests/test_sql_relations.py`가 그대로 확인한다. 오프셋은 파이썬 문자열 인덱스(코드 포인트)다.
위치는 SQL 리터럴 자체를 가리키므로 오프셋 단위 차이는 사실에 드러나지 않는다.

포트 원본은 키워드마다 같은 문장의 앞뒤 토큰과 짝 괄호까지 다시 훑는다. 판정은 그대로 두되 그 답(문장 구간마다
판정 단어의 위치, 괄호 짝)을 처음 쓸 때 한 번 색인해, 세미콜론 없는 긴 문장에서도 토큰 수에 선형으로 읽는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 단일 기호 토큰으로 남기는 문자다. `;`는 문장 경계, 나머지는 피연산자 판정용이다.
_SYMBOL_TOKENS = frozenset(".,();{}$?:@")

#: 읽히지 않은 피연산자의 근거가 되는 플레이스홀더 기호다.
_PLACEHOLDER_SYMBOLS = frozenset("{}$?:@")

#: INTO가 관계 자리임을 확정하는 앞선 동사다.
_INTO_VERBS = frozenset({"insert", "select", "merge", "replace"})

#: CREATE 문에서 ON이 관계 자리가 되는 객체 종류다.
_CREATE_ON_KINDS = frozenset({"index", "trigger", "policy"})

#: `table`을 키워드로 만드는 직전 DDL 동사다.
_TABLE_VERBS = frozenset(
    {"alter", "drop", "create", "truncate", "rename", "lock", "unlock", "describe", "desc", "analyze", "vacuum"}
)

#: GRANT 피연산자 목록을 끝내는 단어다.
_GRANT_TERMINATORS = frozenset({"to", "from", ";"})

#: GRANT ON 뒤 테이블 계열 종류어다.
_GRANT_TABLE_KINDS = frozenset({"table", "tables", "view", "materialized"})

#: GRANT ON 뒤 비테이블 권한 객체 종류어다.
_GRANT_NON_TABLE_KINDS = frozenset(
    {
        "all", "sequence", "schema", "database", "domain", "type", "function", "procedure", "routine", "foreign",
        "server", "wrapper", "language", "large", "publication", "subscription", "statistics", "tablespace",
        "collation", "conversion", "extension", "aggregate", "operator", "policy", "cast", "fdw", "parser",
        "template", "dictionary", "configuration",
    }
)  # fmt: skip

#: 뒤따르는 식별자가 관계 이름인 키워드다.
_RELATION_KEYWORDS = frozenset({"from", "join", "into", "update", "table", "truncate", "on"})

#: SQL 문을 여는 강한 동사다.
_SQL_VERBS = frozenset(
    {
        "select", "insert", "delete", "create", "alter", "drop", "replace", "merge", "lock", "unlock", "rename",
        "describe", "desc", "analyze", "vacuum", "grant", "revoke",
    }
)  # fmt: skip

#: GRANT/REVOKE의 권한 단어다. `ON`이 관계 자리임을 확정하는 근거다.
_GRANT_PRIVILEGES = frozenset(
    {
        "select", "insert", "update", "delete", "truncate", "references", "trigger", "execute", "usage", "create",
        "connect", "temporary", "temp", "maintain", "all",
    }
)  # fmt: skip

#: `_RelationScan._preceded_by`가 찾는 단어 집합이다. 문장 구간마다 각 집합 단어의 첫 위치를 미리 기록하므로,
#: 다른 집합으로 그 판정을 부르려면 여기에 더한다.
_PRECEDING_WORDS = (_INTO_VERBS, _GRANT_PRIVILEGES, _CREATE_ON_KINDS)

#: 관계 이름 위치에 올 수 없는 SQL 절 키워드다. `table`은 `UPDATE table SET` 때문에 제외하고 산문 관사
#: `the`·`an`은 포함한다(`a`는 흔한 별칭이라 제외).
_CLAUSE_WORDS = frozenset(
    {
        "where", "set", "on", "group", "order", "by", "having", "limit", "offset",
        "union", "intersect", "except", "values", "returning", "as", "left",
        "right", "inner", "outer", "full", "cross", "natural", "lateral", "using",
        "and", "or", "not", "null", "select", "insert", "delete", "from", "join",
        "into", "update", "truncate", "with", "for", "in", "is", "case", "when",
        "then", "else", "end", "distinct", "asc", "desc", "if", "exists", "only",
        "between", "like", "to", "grant", "revoke", "option", "cascade",
        "restrict", "privileges", "the", "an",
    }
)  # fmt: skip


@dataclass(frozen=True)
class SqlToken:
    """SQL 어휘 하나. 인용된 식별자는 키워드가 아니다.

    Attributes:
        text: 인용 부호를 벗긴 어휘 본문.
        quoted: `"…"`·`` `…` ``·`[…]`로 인용된 식별자인지.
        offset: 원문에서의 시작 인덱스.
    """

    text: str
    quoted: bool
    offset: int


@dataclass(frozen=True)
class SqlRelation:
    """관계 이름 하나와 그것을 연 키워드 토큰의 원문 위치."""

    name: str
    keyword: int


@dataclass(frozen=True)
class SqlRelationsResult:
    """관계 추출 결과. `unresolved`는 관계 자리의 피연산자를 읽지 못한 횟수다."""

    relations: tuple[SqlRelation, ...]
    unresolved: int


def lex_sql(text: str) -> list[SqlToken]:
    """SQL 텍스트를 어휘로 나눈다.

    인용 식별자는 내용을 보존하고, 그 밖에는 식별자와 단일 기호 토큰만 만든다. 주석과 문자열 리터럴은
    이름이 아니므로 건너뛴다.

    Args:
        text: SQL 텍스트.

    Returns:
        어휘 목록.
    """
    tokens: list[SqlToken] = []
    index = 0
    while index < len(text):
        index = _lex_one(text, index, tokens)
    return tokens


def _lex_one(text: str, index: int, tokens: list[SqlToken]) -> int:
    """위치의 어휘 하나를 읽는다.

    Args:
        text: SQL 텍스트.
        index: 현재 위치.
        tokens: 결과 목록(추가한다).

    Returns:
        다음 위치.
    """
    character = text[index]
    following = text[index + 1] if index + 1 < len(text) else ""
    if character in '"`[':
        return _lex_quoted(text, index, tokens)
    if _is_ident_start(character):
        end = index + 1
        while end < len(text) and _is_ident_part(text[end]):
            end += 1
        tokens.append(SqlToken(text[index:end], False, index))
        return end
    if character == "-" and following == "-":
        newline = text.find("\n", index)
        return len(text) if newline < 0 else newline
    if character == "/" and following == "*":
        return _skip_block_comment(text, index + 2)
    if character == "'":
        return _skip_string_literal(text, index + 1)
    if character in _SYMBOL_TOKENS:
        tokens.append(SqlToken(character, False, index))
    return index + 1


def _lex_quoted(text: str, start: int, tokens: list[SqlToken]) -> int:
    """인용 식별자 하나를 읽어 토큰으로 넣는다.

    Args:
        text: SQL 텍스트.
        start: 여는 인용 부호 위치.
        tokens: 결과 목록(추가한다).

    Returns:
        닫는 부호 다음 위치.
    """
    close = "]" if text[start] == "[" else text[start]
    end = text.find(close, start + 1)
    end = len(text) if end < 0 else end
    tokens.append(SqlToken(text[start + 1 : end], True, start))
    return end + 1


def _skip_block_comment(text: str, start: int) -> int:
    """블록 주석을 건너뛴다.

    Args:
        text: SQL 텍스트.
        start: 주석 본문 시작 위치.

    Returns:
        주석 끝 다음 위치.
    """
    index = start
    while index + 1 < len(text) and not (text[index] == "*" and text[index + 1] == "/"):
        index += 1
    return index + 2


def _skip_string_literal(text: str, start: int) -> int:
    """문자열 리터럴을 건너뛴다. `''`와 `\\'`는 escape다.

    Args:
        text: SQL 텍스트.
        start: 여는 따옴표 다음 위치.

    Returns:
        닫는 따옴표 다음 위치(닫히지 않으면 텍스트 끝).
    """
    index = start
    while index < len(text):
        if text[index] == "\\" or (text[index] == "'" and index + 1 < len(text) and text[index + 1] == "'"):
            index += 2
        elif text[index] == "'":
            return index + 1
        else:
            index += 1
    return index


def looks_like_sql(text: str, strict: bool = False) -> bool:
    """SQL 문을 여는 강한 동사가 있는지 본다.

    관계 키워드와 겹치는 update·truncate는 문장 머리일 때만 인정한다. `strict`는 게이트 없는 문자열
    리터럴용으로, 동사가 대문자일 때만 인정해 산문을 거른다.

    Args:
        text: 검사할 텍스트.
        strict: 대문자 동사만 인정할지.

    Returns:
        SQL로 보이면 True.
    """
    head = True
    for token in lex_sql(text):
        if not _is_name_token(token):
            continue
        upper = token.text == token.text.upper()
        lower = token.text.lower()
        if lower in _SQL_VERBS and (not strict or upper):
            return True
        if head:
            head = False
            if lower in ("update", "truncate") and (not strict or upper):
                return True
    return False


def sql_relations(text: str, strict: bool = False) -> SqlRelationsResult:
    """SQL 텍스트에서 관계 이름을 읽는다.

    한정 이름(`schema.table`)은 그대로 두고, 이름 자체에 점이 있는 인용 식별자(`"a.b"`)는 한 세그먼트로
    escape한다. `FROM {}` 같은 플레이스홀더는 사실 없이 넘기지 않고 미해석으로 센다.

    Args:
        text: SQL 텍스트.
        strict: 관계 키워드와 동사가 대문자일 때만 발화할지.

    Returns:
        관계 목록과 미해석 피연산자 수.
    """
    tokens = lex_sql(text)
    scan = _RelationScan(tokens, strict)
    for index in range(len(tokens)):
        scan.visit_keyword(index)
    return SqlRelationsResult(tuple(scan.out), scan.unresolved)


@dataclass
class _Segment:
    """`;` 토큰으로 나뉜 토큰 구간 하나에서 키워드 판정이 찾는 단어의 위치.

    대문자 게이트를 통과한 비인용 토큰만 기록한다(포트 원본 `_preceded_by`·`_segment_after_has`의 토큰 조건과 같다).

    Attributes:
        first: `_PRECEDING_WORDS`의 단어 집합마다 그 단어가 구간에서 처음 나온 토큰 위치.
        last_set: `set`이 구간에서 마지막으로 나온 토큰 위치(없으면 -1).
    """

    first: dict[frozenset[str], int] = field(default_factory=dict)
    last_set: int = -1

    def record(self, word: str, index: int) -> None:
        """구간의 토큰 하나를 기록한다.

        Args:
            word: 소문자 단어.
            index: 토큰 위치(앞에서부터 차례로 부른다).
        """
        for words in _PRECEDING_WORDS:
            if word in words:
                self.first.setdefault(words, index)
        if word == "set":
            self.last_set = index


@dataclass
class _RelationScan:
    """`sql_relations` 한 번의 스캔 상태. 포트 원본의 필드·메서드와 한 줄씩 대응한다.

    원본이 키워드마다 문장 앞뒤와 짝 괄호를 다시 훑던 세 판정(`_preceded_by`·`_segment_after_has_set`·
    `_skip_parens`)만 처음 쓸 때 한 번 만든 색인(`segment_of`·`segments`·`paren_ends`)으로 답한다. 답은 원본과 같다.
    그 판정을 부르지 않는 흔한 문장(`SELECT … FROM t WHERE …`)은 색인을 만들지 않는다.
    """

    tokens: list[SqlToken]
    strict: bool
    consumed: list[bool] = field(default_factory=list)
    statement_head: list[bool] = field(default_factory=list)
    statement_verb: list[str | None] = field(default_factory=list)
    segment_of: list[int] = field(default_factory=list)
    segments: list[_Segment] = field(default_factory=list)
    paren_ends: list[int | None] = field(default_factory=list)
    seen: set[tuple[int, str]] = field(default_factory=set)
    out: list[SqlRelation] = field(default_factory=list)
    unresolved: int = 0

    def __post_init__(self) -> None:
        """토큰별 상태 배열을 만들고 문장 머리를 표시한다. 문장 구간·괄호 짝 색인은 처음 쓸 때 만든다."""
        self.consumed = [False] * len(self.tokens)
        self.statement_head = [False] * len(self.tokens)
        self.statement_verb = [None] * len(self.tokens)
        self._mark_statements()

    def _upper_ok(self, token: SqlToken) -> bool:
        """strict 모드에서는 대문자 토큰만 통과시킨다.

        Args:
            token: 검사할 토큰.

        Returns:
            게이트를 통과하면 True.
        """
        return not self.strict or token.text == token.text.upper()

    def _is_symbol(self, index: int, symbol: str) -> bool:
        """위치의 토큰이 비인용 기호 `symbol`인지 본다.

        Args:
            index: 토큰 위치.
            symbol: 기호 문자.

        Returns:
            같으면 True.
        """
        return 0 <= index < len(self.tokens) and not self.tokens[index].quoted and self.tokens[index].text == symbol

    def _mark_statements(self) -> None:
        """문장 머리와 동사를 표시한다.

        문장 머리의 `ident :`는 SQLDelight·drift 라벨이라 머리를 차지하지 않고 다음 식별자가 머리가 된다
        (`::` 캐스트는 라벨이 아니다).
        """
        pending = True
        verb: str | None = None
        index = 0
        while index < len(self.tokens):
            token = self.tokens[index]
            if self._is_symbol(index, ";"):
                pending, verb = True, None
                index += 1
                continue
            if _is_name_token(token) and pending:
                if self._is_symbol(index + 1, ":") and not self._is_symbol(index + 2, ":"):
                    index += 2
                    continue
                self.statement_head[index] = True
                verb = token.text.lower() if self._upper_ok(token) else None
                pending = False
            self.statement_verb[index] = verb
            index += 1

    def _index_segments(self) -> None:
        """토큰마다 속한 문장 구간을 정하고 구간마다 판정 단어의 위치를 기록한다.

        구간은 본문이 `;`인 토큰(인용 식별자 `";"`·`[;]` 포함)에서 끊긴다. 포트 원본이 키워드마다 앞뒤로 훑을 때
        멈추던 자리와 같다(문장 머리 표시 `_mark_statements`는 비인용 `;`만 보므로 다르다). `;` 토큰은 앞 구간에
        넣는다. 그 자리에서 앞쪽은 앞 구간이고, 뒤쪽은 바로 끝나서 아무것도 없기 때문이다.
        """
        current = _Segment()
        number = 0
        self.segments.append(current)
        for index, token in enumerate(self.tokens):
            self.segment_of.append(number)
            if token.text == ";":
                current = _Segment()
                self.segments.append(current)
                number = len(self.segments) - 1
            elif not token.quoted and self._upper_ok(token):
                current.record(token.text.lower(), index)

    def _match_parens(self) -> None:
        """비인용 `(` 토큰마다 짝이 맞는 `)` 다음 위치를 한 번에 구한다(닫히지 않으면 None).

        `(`부터 깊이를 세며 앞으로 훑는 원본 `_skip_parens`와 답이 같다. 스택에서 짝 없는 `)`는 앞선 `(`가 모두 닫힌
        뒤에만 나오므로 어느 `(`의 짝도 아니다.
        """
        self.paren_ends = [None] * len(self.tokens)
        opened: list[int] = []
        for index, token in enumerate(self.tokens):
            if token.quoted:
                continue
            if token.text == "(":
                opened.append(index)
            elif token.text == ")" and opened:
                self.paren_ends[opened.pop()] = index + 1

    def _skip_parens(self, start: int) -> int | None:
        """`(` 토큰부터 짝이 맞는 `)` 다음 위치를 돌려준다.

        Args:
            start: 비인용 `(` 위치(토큰이 있으므로 색인을 만들면 `paren_ends`가 비지 않는다).

        Returns:
            `)` 다음 위치. 닫히지 않으면 None.
        """
        if not self.paren_ends:
            self._match_parens()
        return self.paren_ends[start]

    def _segment(self, index: int) -> _Segment:
        """토큰이 속한 문장 구간을 돌려준다(색인은 처음 부를 때 만든다. 만들면 구간이 하나 이상이다).

        Args:
            index: 토큰 위치.

        Returns:
            문장 구간.
        """
        if not self.segments:
            self._index_segments()
        return self.segments[self.segment_of[index]]

    def _segment_after_has_set(self, start: int) -> bool:
        """`start`부터 같은 문장 구간 안에 대문자 게이트를 통과한 비인용 `set`이 있는지 본다.

        Args:
            start: 시작 위치.

        Returns:
            있으면 True.
        """
        return start < len(self.tokens) and self._segment(start).last_set >= start

    def _preceded_by(self, index: int, words: frozenset[str]) -> bool:
        """같은 문장 구간 안의 앞선 토큰 중 대문자 게이트를 통과한 비인용 `words` 단어가 있는지 본다.

        Args:
            index: 기준 위치.
            words: `_PRECEDING_WORDS`의 단어 집합.

        Returns:
            있으면 True.
        """
        first = self._segment(index).first.get(words)
        return first is not None and first < index

    def _fires(self, index: int, word: str, grant_statement: bool) -> bool:
        """키워드 토큰이 현재 문맥에서 관계 자리를 여는지 판정한다.

        Args:
            index: 키워드 위치.
            word: 소문자 키워드.
            grant_statement: GRANT/REVOKE 문장인지.

        Returns:
            관계 자리를 열면 True.
        """
        if word == "update":
            # 산문 "update the .."·upsert의 `DO UPDATE SET`을 막는다.
            return self.statement_head[index] and self._segment_after_has_set(index + 1)
        if word == "truncate":
            return self.statement_head[index]
        if word == "into":
            # "merged the branch into main" 같은 산문을 막는다.
            return self._preceded_by(index, _INTO_VERBS)
        if word == "table":
            return self._table_keyword_context(index)
        if word == "on":
            return self._on_fires(index, grant_statement)
        # from·join: grant·revoke의 FROM은 권한 주체 자리다.
        return not grant_statement

    def _on_fires(self, index: int, grant_statement: bool) -> bool:
        """`ON`은 `GRANT .. ON t`와 `CREATE INDEX/TRIGGER/POLICY .. ON t`만 관계 자리다.

        Args:
            index: 키워드 위치.
            grant_statement: GRANT/REVOKE 문장인지.

        Returns:
            관계 자리를 열면 True.
        """
        grant_on = grant_statement and self._preceded_by(index, _GRANT_PRIVILEGES)
        create_on = self.statement_verb[index] == "create" and self._preceded_by(index, _CREATE_ON_KINDS)
        return grant_on or create_on

    def _table_keyword_context(self, index: int) -> bool:
        """`table` 토큰은 직전 비인용 식별자가 DDL 동사일 때만 키워드다.

        Args:
            index: `table` 토큰 위치.

        Returns:
            키워드면 True.
        """
        for previous in range(index - 1, -1, -1):
            token = self.tokens[previous]
            if not _is_name_token(token):
                continue
            if self.strict and token.text != token.text.upper():
                return False
            return token.text.lower() in _TABLE_VERBS
        return False

    def visit_keyword(self, index: int) -> None:
        """한 토큰이 관계 키워드면 뒤의 피연산자 목록을 읽는다.

        Args:
            index: 토큰 위치.
        """
        keyword = self.tokens[index]
        word = keyword.text.lower()
        if self.consumed[index] or keyword.quoted or word not in _RELATION_KEYWORDS or not self._upper_ok(keyword):
            return
        verb = self.statement_verb[index]
        grant_statement = verb in ("grant", "revoke")
        if not self._fires(index, word, grant_statement):
            return
        after_modifiers = self._skip_modifiers(index + 1, word == "truncate")
        buffered = word == "on" and grant_statement
        next_index = self._skip_grant_object_kind(after_modifiers) if buffered else after_modifiers
        if next_index is None:
            return
        if next_index >= len(self.tokens):
            self.unresolved += 1  # 이름이 없는 키워드 — "SELECT ... FROM" 꼴.
            return
        self._read_operands(next_index, keyword, buffered)

    def _skip_modifiers(self, start: int, after_truncate: bool) -> int:
        """ONLY·IF NOT EXISTS 같은 수식어를 건너뛴다. `table`은 TRUNCATE 뒤에서만 수식어다.

        Args:
            start: 키워드 다음 위치.
            after_truncate: TRUNCATE 뒤인지.

        Returns:
            수식어 다음 위치.
        """
        index = start
        while (
            index < len(self.tokens)
            and not self.tokens[index].quoted
            and _is_name_modifier(self.tokens[index].text, after_truncate)
        ):
            self.consumed[index] = True
            index += 1
        return index

    def _skip_grant_object_kind(self, start: int) -> int | None:
        """GRANT/REVOKE ON 뒤의 객체 종류어를 처리한다.

        테이블 계열이면 이름 위치를, 비테이블 권한 객체면 이름까지 삼키고 None을 돌려준다(사실을 내지 않는다).

        Args:
            start: 종류어 위치.

        Returns:
            이름 위치 또는 None.
        """
        if start >= len(self.tokens) or self.tokens[start].quoted:
            return start
        kind = self.tokens[start].text.lower()
        if kind in _GRANT_TABLE_KINDS:
            index = start
            while index < len(self.tokens) and self.tokens[index].text.lower() in _GRANT_TABLE_KINDS:
                self.consumed[index] = True
                index += 1
            return index
        if kind not in _GRANT_NON_TABLE_KINDS:
            return start
        self._swallow_non_table_object(start)
        return None

    def _swallow_non_table_object(self, start: int) -> None:
        """비테이블 권한 객체의 이름(괄호 인자 포함)을 삼킨다.

        Args:
            start: 종류어 위치.
        """
        index = start
        while index < len(self.tokens):
            token = self.tokens[index]
            if not token.quoted and token.text == "(":
                after = self._skip_parens(index)
                if after is None:
                    self.unresolved += 1  # 닫히지 않은 괄호.
                    return
                index = after
            elif _is_name_token(token) or (not token.quoted and token.text == "."):
                self.consumed[index] = True
                index += 1
            else:
                # 플레이스홀더 피연산자(`ON SEQUENCE {s}`)는 읽히지 않은 근거다.
                if not token.quoted and token.text in _PLACEHOLDER_SYMBOLS:
                    self.unresolved += 1
                return

    def _read_operands(self, start: int, keyword: SqlToken, buffered_grant: bool) -> None:
        """쉼표로 이어지는 피연산자 목록(`FROM a, b`)을 읽는다.

        괄호 피연산자는 통째로 건너뛰고(안쪽 관계는 그 안의 키워드가 읽는다) 별칭은 삼킨다.

        Args:
            start: 첫 피연산자 위치.
            keyword: 목록을 연 키워드.
            buffered_grant: GRANT 형태 확인 뒤에만 내보낼지.
        """
        buffer: list[str] = []
        index = start
        end_position = index
        while index < len(self.tokens):
            operand_end = self._read_operand(index, keyword, buffer if buffered_grant else None)
            if operand_end is None:
                break
            after_alias = self._skip_alias(operand_end)
            end_position = after_alias
            if not self._is_symbol(after_alias, ","):
                break
            index = after_alias + 1
        if buffered_grant and self._grant_terminator_at(end_position):
            for name in buffer:
                self._emit(name, keyword)

    def _read_operand(self, index: int, keyword: SqlToken, buffer: list[str] | None) -> int | None:
        """피연산자 하나를 읽는다.

        Args:
            index: 피연산자 위치.
            keyword: 목록을 연 키워드.
            buffer: GRANT 버퍼(있으면 즉시 내보내지 않고 모은다).

        Returns:
            피연산자 끝 위치. 목록이 끝났으면 None.
        """
        if self._is_symbol(index, "("):
            after = self._skip_parens(index)
            if after is None:
                self.unresolved += 1  # 닫히지 않은 괄호.
            return after
        read = _read_qualified_name(self.tokens, index)
        if read is None:
            # 이름 자리에 절 키워드가 오는 것은 정상 종료다 — 플레이스홀더 등 읽히지 않는 피연산자만 센다.
            token = self.tokens[index] if index < len(self.tokens) else None
            clause_next = token is not None and _is_name_token(token) and token.text.lower() in _CLAUSE_WORDS
            if not clause_next:
                self.unresolved += 1
            return None
        name, after_name = read
        # FROM/JOIN의 함수는 관계 신원이 아니다. INSERT의 컬럼 목록은 그대로 읽는다.
        if keyword.text.lower() in {"from", "join"} and self._is_symbol(after_name, "("):
            self.unresolved += 1
            for consumed in range(index, after_name):
                self.consumed[consumed] = True
            return self._skip_parens(after_name)
        if buffer is None:
            self._emit(name, keyword)
        else:
            buffer.append(name)
        for consumed in range(index, after_name):
            self.consumed[consumed] = True
        return after_name

    def _skip_alias(self, operand_end: int) -> int:
        """`AS alias` 또는 쉼표 직전 별칭(`FROM users u, ..`)을 건너뛴다.

        Args:
            operand_end: 피연산자 끝 위치.

        Returns:
            별칭 다음 위치.
        """
        index = operand_end
        token = self.tokens[index] if index < len(self.tokens) else None
        following = self.tokens[index + 1] if index + 1 < len(self.tokens) else None
        if (
            token is not None
            and not token.quoted
            and token.text.lower() == "as"
            and following is not None
            and _is_name_token(following)
        ):
            index += 2
        elif token is not None and _is_name_token(token) and self._is_symbol(index + 1, ","):
            index += 1
        for consumed in range(operand_end, index):
            self.consumed[consumed] = True
        return index

    def _grant_terminator_at(self, end_position: int) -> bool:
        """GRANT 피연산자 뒤가 TO·FROM·`;`·끝이어야 산문이 아닌 GRANT 형태다.

        Args:
            end_position: 피연산자 목록 끝 위치.

        Returns:
            GRANT 형태면 True.
        """
        if end_position >= len(self.tokens):
            return True
        token = self.tokens[end_position]
        return not token.quoted and token.text.lower() in _GRANT_TERMINATORS

    def _emit(self, name: str, keyword: SqlToken) -> None:
        """관계 하나를 결과에 넣는다. 같은 키워드의 같은 이름은 한 번만 넣는다.

        Args:
            name: 관계 이름.
            keyword: 목록을 연 키워드.
        """
        key = (keyword.offset, name)
        if key in self.seen:
            return
        self.seen.add(key)
        self.out.append(SqlRelation(name, keyword.offset))


def _is_name_modifier(word: str, after_truncate: bool) -> bool:
    """관계 키워드와 이름 사이에 올 수 있는 수식어인지 본다.

    Args:
        word: 단어.
        after_truncate: TRUNCATE 뒤인지(`table`은 그때만 수식어다).

    Returns:
        수식어면 True.
    """
    lower = word.lower()
    return lower in ("only", "if", "not", "exists") or (after_truncate and lower == "table")


def _read_qualified_name(tokens: list[SqlToken], start: int) -> tuple[str, int] | None:
    """`ident(.ident)*` 한정 이름을 읽는다.

    Args:
        tokens: 어휘 목록.
        start: 시작 위치.

    Returns:
        (escape한 이름, 다음 위치). 이름이 아니면 None.
    """
    if start >= len(tokens) or not _is_segment(tokens[start]):
        return None
    name = _escape_segment(tokens[start])
    index = start + 1
    while index + 1 < len(tokens) and tokens[index].text == "." and not tokens[index].quoted:
        following = tokens[index + 1]
        if not following.quoted and not _is_segment(following):
            break
        name += "." + _escape_segment(following)
        index += 2
    return name, index


def _is_segment(token: SqlToken) -> bool:
    """이름 세그먼트가 될 수 있는 토큰인지 본다(빈 인용 식별자와 절 키워드는 아니다).

    Args:
        token: 토큰.

    Returns:
        세그먼트면 True.
    """
    if token.quoted:
        return len(token.text) > 0
    return _is_name_token(token) and token.text.lower() not in _CLAUSE_WORDS


def _escape_segment(token: SqlToken) -> str:
    """인용 세그먼트의 `%`와 `.`을 escape한다. 비인용 세그먼트는 점이 없어 `%`만 escape한다.

    Args:
        token: 세그먼트 토큰.

    Returns:
        escape한 세그먼트.
    """
    return escape_name(token.text) if token.quoted else token.text.replace("%", "%25")


def escape_qualified(name: str) -> str:
    """한정 이름의 각 세그먼트를 escape해 합친다. `.`는 한정자라는 계약과 맞춘다.

    Args:
        name: 한정 이름.

    Returns:
        escape한 이름.
    """
    return ".".join(segment.replace("%", "%25") for segment in name.split("."))


def escape_name(name: str) -> str:
    """이름 문자열 그대로를 한 세그먼트로 escape한다. 이름 자체에 든 `.`는 한정자가 아니다.

    Args:
        name: 식별자.

    Returns:
        escape한 세그먼트.
    """
    return name.replace("%", "%25").replace(".", "%2E")


def _is_name_token(token: SqlToken) -> bool:
    """비인용 식별자 토큰인지 본다.

    Args:
        token: 토큰.

    Returns:
        식별자면 True.
    """
    return not token.quoted and len(token.text) > 0 and _is_ident_start(token.text[0])


def _is_ident_start(character: str) -> bool:
    """SQL 식별자 시작 문자인지 본다. 비ASCII는 식별자로 본다.

    Args:
        character: 문자 하나.

    Returns:
        시작 문자면 True.
    """
    return character in "_$" or ("a" <= character <= "z") or ("A" <= character <= "Z") or ord(character) >= 0x80


def _is_ident_part(character: str) -> bool:
    """SQL 식별자 구성 문자인지 본다.

    Args:
        character: 문자 하나.

    Returns:
        구성 문자면 True.
    """
    return _is_ident_start(character) or ("0" <= character <= "9")
