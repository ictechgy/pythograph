# pythograph

[한국어](https://github.com/ictechgy/pythograph/blob/main/README.ko.md)

Static facts for Python services (Django, Django REST framework, Flask, SQLAlchemy), emitted in the
[isthmus](https://github.com/ictechgy/isthmus) bridge-facts exchange format.

pythograph is the Python member of a family of static-analysis CLIs (tsograph for
TypeScript/JavaScript, cartograph for Swift, kartograph for Kotlin, dartograph for Dart, gartograph for
Go, rustograph for Rust, schemagraph for SQL). Each tool reports only what it observes in its own language;
isthmus joins the documents.

The analyzed project is parsed with the standard-library `ast` only. It is never imported or executed,
and pythograph has no runtime dependencies and uses no network (`graph`, `reach`, and `impact` run only the project
root's git to read `revision`).

## Status

| Area | State |
|---|---|
| `pythograph routes --role server`: Django URLconf, Django REST framework routers and views, Flask/Werkzeug rules → `route-decl` facts | Implemented |
| `pythograph schema`: Django models and QuerySets, SQLAlchemy 2.x / Flask-SQLAlchemy 3 mappings and queries, SQL text → persistence `relation-use` facts | Implemented |
| `pythograph graph` / `reach` / `impact`: Python call graph → isthmus `language-traversal` v1 (evidence tiers `direct`/`candidate`, `unresolvedCalls`, Django/DRF/Flask dispatch) | Implemented |
| Client route-calls (requests, httpx) | Planned |

isthmus `main` (`f9dcd1d`) accepts `platform: "python"` http documents (including `registration-order`), persistence
documents, and python `language-traversal` analyses (see [isthmus compatibility](#isthmus-compatibility)).

## Requirements and installation

- Python 3.10 or newer (Django 5.x needs 3.10+, and pythograph parses the analyzed code with the running
  interpreter, so run it with the same or a newer Python than the project).

Install from [PyPI](https://pypi.org/project/pythograph/):

```sh
uv tool install pythograph
# or
pipx install pythograph

pythograph --version
```

To try unreleased changes, install from GitHub (`uv tool install git+https://github.com/ictechgy/pythograph`, or pin a
release tag such as `@v0.1.0`). A wheel built from a checkout (`uv build`) installs the same way:
`uv tool install dist/pythograph-<version>-py3-none-any.whl`. The release steps are in
[RELEASING.md](https://github.com/ictechgy/pythograph/blob/main/RELEASING.md) (Korean).

## `pythograph routes --role server`

```sh
pythograph routes --role server --project <root> [--service <name>] [--include-tests]
                  [--framework auto|django|flask] [--settings <module>]
                  [--dispatch specificity] [--generated-at <timestamp>] [--format json]
```

Writes a bridge-facts v1 document to stdout: `platform: "python"`, `target: "http"`, `roles: ["server"]`,
`dispatch`, `sourceSets`, and one `route-decl` fact per (route, HTTP method).

- `--project` (required): the project root. `project` is its POSIX realpath and every `location.path` is
  relative to it.
- `--framework`: `auto` (default) detects Django (a `DJANGO_SETTINGS_MODULE` default in `manage.py`,
  `wsgi.py`, or `asgi.py`, or `--settings`) and Flask (a `Flask(...)` or `Blueprint(...)` object). A project
  with both is a usage error until you pick one.
- `--settings`: the Django settings module when the entry files do not name it.
- `--include-tests`: also emit routes declared in test sources (`test_*.py`, `*_test.py`, `tests.py`,
  `conftest.py`, files under `tests/` or `test/`) with `testSource: true` and `sourceSets.tests: "included"`.
- `--dispatch specificity`: declare `specificity` for a Django project and omit `order` (see
  [Decisions](#decisions)).
- `--generated-at`: a fixed `generatedAt` for byte-identical output.
- Exit codes: `0` success (zero facts is still success, not proof of completeness), `2` unreadable project,
  more than 100,000 facts, output over 16 Mi characters, or an internal error (the message states the cause
  and a fix, never source text or absolute paths), `64` usage error. `1` is reserved.

Example (synthetic, compacted; the real output is key-sorted JSON with two-space indentation):

```json
{
  "dispatch": "registration-order",
  "facts": [
    {
      "channel": "/catalog/items/{}/edit/",
      "dynamic": false,
      "kind": "route-decl",
      "location": { "column": 10, "line": 20, "path": "catalog/urls.py" },
      "method": "POST",
      "order": { "group": "django:shop.urls", "index": 7 },
      "paramConstraints": [{ "kind": "int", "segment": 2 }],
      "pathAnchor": "root",
      "symbol": { "qualifiedName": "catalog/views.py#ItemEditView.post", "usr": "catalog/views.py#ItemEditView.post" },
      "trailingSlash": "strict"
    }
  ],
  "format": "bridge-facts",
  "platform": "python",
  "roles": ["server"],
  "target": "http",
  "tool": { "name": "pythograph", "version": "0.1.0" },
  "version": 1
}
```

### What is modeled

Every rule was checked against the installed package sources (Django 5.2.17, djangorestframework 3.18.1,
Flask 3.1.3, Werkzeug 3.1.9). The full table with source files is in [docs/HTTP-ROUTES.md](https://github.com/ictechgy/pythograph/blob/main/docs/HTTP-ROUTES.md)
(Korean).

- **Django**: `ROOT_URLCONF` from the settings module (star imports of project settings modules are
  followed), `urlpatterns` built with lists, `+`, `+=`, `.append`, `.extend`, `.insert`, `path()`, `re_path()`,
  `include()` (module strings, module objects, lists, `(patterns, app_name)` tuples, nested), default and
  registered path converters, `re_path` regular expressions converted from their syntax tree when possible,
  function views (`ANY`, narrowed by `require_http_methods`/`require_GET`/`require_POST`/`require_safe`/
  `api_view`), class views (`as_view()`, handlers along the class chain, `http_method_names`), and the
  registration order (first match wins).
- **Django REST framework**: `SimpleRouter`/`DefaultRouter` (`trailing_slash`, `use_regex_path`, lookup
  settings, `@action` with `detail`, `methods`, `url_path`, and `.mapping`), the `DefaultRouter` API root and
  format-suffix variants, `format_suffix_patterns`, `APIView`, generic views, and viewsets.
- **Flask**: `Flask(...)` and `Blueprint(...)` objects at module level or in app factories, `@route`,
  `@get`/`@post`/`@put`/`@delete`/`@patch`, `add_url_rule`, `register_blueprint` (nested, `url_prefix`),
  `MethodView`/`View`, Werkzeug converters (`string`, `int`, `float`, `uuid`, `path`, `any`, custom),
  `strict_slashes`, and `merge_slashes`.

### How facts are built

- **channel**: the canonical path template. A parameter filling a whole segment is `{}`, a partial segment
  keeps its literal skeleton (`/files/{}.json`), a final parameter that can match `/` is `{**}`, and anything
  else that cannot be proven (a segment with two parameters, a middle catch-all, lookarounds, unanchored
  regular expressions) is `dynamic` with a `route-coverage:` limitation. Literals are in the decoded path
  space, so non-pchar characters are UTF-8 percent-encoded and `%` becomes `%25`.
- **method**: uppercase verbs or `ANY`. `HEAD` next to `GET` and automatic `OPTIONS` are not emitted (isthmus
  matches them with `head-as-get` and `options-any`); an explicitly declared `OPTIONS` is.
- **paramConstraints**: `int`, `slug`, `uuid`, `path` (for `{**}`), or `regex` with the pattern.
- **trailingSlash**: Django and strict Flask rules are `strict`; `/?` in a regex and Flask
  `strict_slashes=False` are `optional`; omitted after `{**}`.
- **order** (Django): `{group: "django:<ROOT_URLCONF>", index: <depth-first position>}`.
- **location**: the route string argument (Django `path()`/`re_path()`, Flask decorator or `add_url_rule`), the
  `router.register()` prefix for DRF routes, or the `@action` decorator for extra actions; 1-based line and
  1-based UTF-8 byte column.

### Symbol ids

`symbol.usr` is `<project-relative POSIX path>#<lexical dotted name>`, outermost declaration first and
without `<locals>`: `catalog/views.py#item_list`, `blog/__init__.py#create_app.index`,
`catalog/views.py#ItemEditView.get`, `orders/views.py#OrderViewSet.list`. Class handlers are named after the
class registered in the URL even when the method is inherited; the call graph (`pythograph graph`) has an
inherited-member node with the same id. Views defined outside the project have no usr and are counted under
`missing-route-usrs:`.

### Limitations

When a value cannot be proven, pythograph does not guess: it emits `dynamic`, `pathAnchor: "base"`, or a
limitation with one of the contract's prefixes, and adds `limitationScopes` only when it can prove an upper
bound. Examples: conditional registrations (`if settings.DEBUG:`) become `route-coverage:` scoped to their
templates; unresolved includes and third-party URL modules are scoped to their include prefix; the Django admin,
`static()`, `django.contrib.staticfiles`, and Flask static files are `framework-provided-routes:` with prefix
scopes (Flask static also with `GET`/`HEAD`); `FORCE_SCRIPT_NAME`, `i18n_patterns`, and blueprints whose
registration is not visible use a `base` anchor with `unresolved-route-prefix:`; a project that does not pin
Django 5, DRF 3, or Flask 3 gets `route-framework-version-unknown:`.

### Decisions

- **Django is `registration-order`, Flask is `specificity`**, as verified from the sources. isthmus releases
  before `f9dcd1d` reject `registration-order`; `--dispatch specificity` declares specificity for Django and omits
  `order`. That approximation can only produce false matches (a shadowed pattern matched), never false errors,
  because isthmus filters by method first and reports a method mismatch only when no candidate accepts the
  method, which is also when Django answers 405.
- **Shadowed patterns are still declarations**; shadowing is the consumer's judgement from `order`.
- **Conditional registrations are scoped limitations**, not declarations.

## `pythograph schema`

```sh
pythograph schema --project <root> [--include-tests] [--settings <module>] [--generated-at <timestamp>] [--format json]
```

Writes a bridge-facts v1 document with `platform: "python"`, `target: "persistence"` (or `null` when there are no
facts), and one `relation-use` fact per observed relation or column reference. isthmus joins it with a
`platform: "sql"` document (schemagraph `facts --document <catalog>`) under the persistence rules of
`docs/GRAPH-EXCHANGE.md`. The full rule table with source files is in [docs/PERSISTENCE.md](https://github.com/ictechgy/pythograph/blob/main/docs/PERSISTENCE.md)
(Korean).

- **Django**: model classes (abstract, proxy, multi-table inheritance, `Meta` inheritance) → tables
  (`<app_label>_<model>` truncated by `truncate_name` for the backend's `max_name_length`, or `Meta.db_table`), fields →
  columns (`db_column`, `<name>_id` for foreign keys, many-to-many tables and their columns), app labels from
  `INSTALLED_APPS`/`AppConfig`, the `DATABASES` backend, and django.contrib models. Uses: managers and QuerySet chains,
  lookups (`author__profile__city`, reverse relations, `attname`, `pk`), `values`/`order_by`/`F`/`Q`/aggregates,
  `create`/`update` keywords, related managers and forward relations on proven instances, `raw()`, `RawSQL`,
  `extra(tables=)`, and cursor SQL.
- **SQLAlchemy 2.x / Flask-SQLAlchemy 3**: Declarative classes (`DeclarativeBase`, `declarative_base()`, `db.Model`
  with its snake_case names), mixins, single- and joined-table inheritance, `__table_args__`/`MetaData` schemas, Core
  `Table`, `ForeignKey("t.c")`, `relationship(secondary=)`. Uses: statement entities (`select`, `insert`, `update`,
  `delete`, `session.query`, `session.get`, `join`), `Model.column`, `Model.relationship`, `Model.query`, `filter_by`,
  constructors, `table.c.name`, and `text()`.
- **SQL text**: the family's shared lexical extractor (the same vectors as tsograph, dartograph, cartograph, and
  kartograph) for explicit SQL arguments, and uppercase SQL literals elsewhere (docstrings are skipped).
- **channel** is the relation name as written or mapped (`schema.table` only when qualified; no default schema is
  guessed), **method** is the column, and **symbol.usr** is the enclosing function, method, or model class with the
  same ids as `routes`. A name that depends on an unknown backend, app label, or Flask-SQLAlchemy version, an
  unresolved model or lookup, and SQL built at runtime become `dynamic` facts with a `dynamic-relation-names:`
  limitation instead of guesses.
- Test sources (unless `--include-tests`) and migrations (Django `migrations/`, Alembic `versions/`) are not scanned;
  migrations describe past schemas.

## `pythograph graph` / `reach` / `impact`

```sh
pythograph graph  --project <root> [--include-tests] [--revision <id>] [--generated-at <timestamp>]
pythograph reach  --project <root> [--dispatch direct|bound|candidates] [--max-depth <n>] [--max-reached <n>]
                  [--roots-from <file|->] [--include-tests] [--revision <id>] [--generated-at <timestamp>] [--] <id>...
pythograph impact (same options as reach)
```

Builds the project's Python call graph with the standard-library `ast`. `graph` writes pythograph's own snapshot
(`pythograph-graph` v1); `reach` writes the symbols the roots depend on (`dependencies`) and `impact` the symbols that
depend on them (`dependents`) as isthmus
[`language-traversal` v1](https://github.com/ictechgy/isthmus/blob/main/docs/LANGUAGE-TRAVERSAL.md). Ids are the same
strings as `symbol.usr` in `routes` and `schema`. The full rules are in [docs/GRAPH.md](https://github.com/ictechgy/pythograph/blob/main/docs/GRAPH.md) (Korean).

- **Nodes**: modules (`<path>#<module>`), functions, methods, classes, nested definitions, and inherited members
  (`<registered class>.<member>`, for view handlers and for inherited members called on exact receivers).
- **Edges**: `call`, `new` (the project `__init__`, else the class), `callback`, `reference`, `decorator`, `attribute`
  (class-body attributes), `inherit`, and `dispatch`/`framework` (framework dispatch). Resolution follows imports
  (absolute, relative, aliases, `__init__` re-exports, `*`), module attributes, constructors, `self` and `super()`
  through the C3 MRO of project classes, annotated and return-annotated receivers, module-level instances, and
  properties.
- **Evidence tiers**: statically resolved edges are `direct`; overrides in project subclasses for `self`, `cls`, and
  annotated receivers are `candidate`. `bound` edges are not produced yet, so `--dispatch bound` follows the `direct`
  graph.
- **No guessing**: calls without a known target are counted by reason (`parameter`, `untyped-receiver`,
  `dynamic-attribute`, `getattr`, `dynamic-callee`, `unresolved-import`, `framework-callback`, …) as each symbol's
  `unresolvedCalls`. A method call on an untyped receiver is proven external only when no project class, module, or
  attribute write defines that name.
- **Framework dispatch**: a table read with `ast` from the installed Django 5.2.17, DRF 3.18.1, and Flask 3.1.3
  sources links the project hooks that the `as_view()` dispatch path (`dispatch`, `initial`, permission checks,
  `__init__`) and framework implementations (`ModelViewSet.retrieve` → `get_object` → `get_queryset`,
  `ModelSerializer.save` → `create`) call. Objects the framework builds from class attributes (`serializer_class`,
  `permission_classes`) count as `framework-callback` unresolved calls.
- **Traversal documents**: a `dispatch` declaration, per-root lower-bound `evidence`, `unresolvedCalls`, one
  multi-root pass (compared with a per-root oracle on random graphs), `--max-depth`/`--max-reached` truncation, and
  `rootsTruncated`. Roots that are not graph nodes are listed without `symbol`; the document is written and the
  command exits `64`. `revision` is `--revision` or git `HEAD` when the work tree is clean; `graphRevision` is a
  SHA-256 of the graph content.

## Validation

The oracle harness in `experiments/oracle/` imports the synthetic fixtures in a scratch virtual environment
and compares pythograph's facts with Django's resolver traversal, DRF routers, and Flask's `url_map`
(`tests/test_fixtures.py` replays the recorded results offline):

| Target | Precision | Recall |
|---|---|---|
| `fixtures/django/drf-shop` | 69/69 | 58/59 (one intentional dynamic lookahead pattern) |
| `fixtures/flask/blog-app` | 28/28 | 27/27 |
| HackSoftware/Django-Styleguide-Example `a70ef43` (MIT, scratch clone) | 21/21 | 21/22 (the DEBUG-only `static()` route) |

Dogfooding on four public apps (Django-Styleguide-Example, babybuddy, microblog, and netbox, cloned only into a scratch
directory) measured route precision against each framework's resolver, relation-use join rates through schemagraph and
isthmus, handler-to-relation reachability, unresolved-call reasons, and runtime; the results and the fixed issues are in
[DOGFOOD.md](https://github.com/ictechgy/pythograph/blob/main/DOGFOOD.md) (Korean).

The isthmus shared conformance vectors (`conformance/`, vendored from isthmus `76b6141` and locked in
`conformance.lock`) pass 100% of the applicable producer cases (78: `template.grammar`, `template.normalize`,
`scope.validate`, `scope.applies`, `dispatch.validate`); the `dispatch.validate` checker also runs on the routes
golden output.

**Phase 6 exit criterion (Django backend × iOS/Android chain).** `experiments/e2e/` joins a synthetic Django + DRF
server (`fixtures/e2e/shop-api`), a schemagraph catalog of its Django DDL, and route-calls plus reverse traversals of
synthetic iOS (cartograph) and Android (kartograph) clients with isthmus `trace` (workspace). The expected paths of the
three questions match: (a) API → DB tables + DB dependents, (b) API → client call sites → affected client symbols, and
(c) table → API → client. The Android recording needs kartograph `4c09d91` or later (Retrofit route-call usrs and
`baseUrl` joins), which attaches the Android order and checkout calls to the chain. `tests/test_e2e_trace.py` re-checks
the recorded inputs and outputs offline (the table is in
[docs/GRAPH.md](https://github.com/ictechgy/pythograph/blob/main/docs/GRAPH.md#phase-6-종료-조건-django-백엔드--iosandroid-체인)).

Persistence naming vectors (`fixtures/persistence-naming/vectors.json`) are recorded by importing synthetic models
with the real ORMs in a scratch environment (`experiments/persistence/run_naming.py`): Django 5.2.17 `_meta` names
quoted by each backend's `connection.ops` (sqlite3, postgresql, mysql, oracle), and SQLAlchemy 2.0.54 /
Flask-SQLAlchemy 3.1.1 mappers. pythograph matches 100% (Django 136/136 model-backend pairs, SQLAlchemy 9/9 and
Flask-SQLAlchemy 10/10 classes, all tables and columns). Joining `pythograph schema` output for the two persistence
fixtures with schemagraph catalogs of the DDL the ORMs create (`experiments/persistence/run_e2e.py`) gives no isthmus
errors (41 and 20 matches).

## isthmus compatibility

isthmus `main` (`f9dcd1d`, #128) accepts `platform: "python"`: http `route-decl` facts (Django's `registration-order`
and `order`, with shadowing diagnostics), persistence `relation-use` facts, and python `forward`/`reverse` analyses
(`language-traversal` v1) in `trace`. `--dispatch specificity` remains for older isthmus releases.

## Development

```sh
uv sync
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
uv run pytest --cov          # line and branch coverage gate: 90%
uv run python scripts/verify_cli_contract.py
```

The framework hook table is regenerated from a scratch virtual environment with Django 5.2.17, DRF 3.18.1, and
Flask 3.1.3 installed: `python experiments/graph/dump_framework_hooks.py --site-packages <path>` (`--check` compares
only).

## License

MIT
