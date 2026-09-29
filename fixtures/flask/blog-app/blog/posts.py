"""글 블루프린트다. 블루프린트 자체에 url_prefix가 있다."""

from flask import Blueprint

# `/posts` 접두사의 글 블루프린트다.
posts_bp = Blueprint("posts", __name__, url_prefix="/posts")


@posts_bp.get("/")
def list_posts():
    """글 목록이다."""
    return "posts"


@posts_bp.post("/")
def create_post():
    """글을 만든다."""
    return "created"


@posts_bp.get("/<int:post_id>")
def show_post(post_id):
    """글 상세다."""
    return str(post_id)


@posts_bp.delete("/<int:post_id>")
def delete_post(post_id):
    """글을 지운다."""
    return ""


@posts_bp.route("/<any(draft, published):state>/list")
def posts_by_state(state):
    """any 변환기 규칙이다."""
    return state


@posts_bp.route("/by-uuid/<uuid:u>", methods=["GET", "PUT"])
def post_by_uuid(u):
    """uuid 변환기 규칙이다(GET, PUT)."""
    return str(u)
