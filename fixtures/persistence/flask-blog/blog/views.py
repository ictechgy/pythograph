"""블로그 화면: Flask-SQLAlchemy·SQLAlchemy 2 질의."""

from flask import Blueprint
from sqlalchemy import func, select, text, update

from blog.extensions import db
from blog.models import BlogPost, Comment, Tag, User

bp = Blueprint("blog", __name__)


@bp.get("/users/<email>")
def user_by_email(email):
    """이메일로 사용자를 찾는다."""
    return User.query.filter_by(email=email).first_or_404()


@bp.get("/users/<int:user_id>/posts")
def user_posts(user_id):
    """사용자의 글."""
    statement = select(BlogPost).where(BlogPost.author_id == user_id).order_by(BlogPost.created.desc())
    return db.session.execute(statement).scalars().all()


@bp.get("/posts/<int:post_id>")
def post_detail(post_id):
    """글 하나."""
    return db.get_or_404(BlogPost, post_id)


@bp.post("/posts/<int:post_id>/comments")
def add_comment(post_id):
    """댓글을 더한다."""
    db.session.add(Comment(post_id=post_id, body="hello"))
    db.session.commit()


@bp.get("/tags/<name>")
def tagged(name):
    """태그로 거른 글."""
    return BlogPost.query.join(BlogPost.tags).filter(Tag.name == name).all()


@bp.get("/authors")
def authors():
    """글이 있는 작성자와 글 수."""
    rows = db.session.query(User.display_name, func.count(BlogPost.id)).join(BlogPost).group_by(User.id)
    return rows.all()


@bp.post("/posts/<int:post_id>/retitle")
def retitle(post_id):
    """제목을 바꾼다."""
    db.session.execute(update(BlogPost).where(BlogPost.id == post_id).values(title="renamed"))


@bp.get("/stats")
def stats():
    """원시 SQL 통계."""
    return db.session.execute(text("SELECT COUNT(*) FROM post_tags")).scalar()
