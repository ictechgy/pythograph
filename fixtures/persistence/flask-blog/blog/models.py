"""블로그 모델(Flask-SQLAlchemy 3 자동 테이블 이름)."""

from datetime import datetime

from blog.extensions import db

post_tags = db.Table(
    "post_tags",
    db.Column("post_id", db.ForeignKey("blog_post.id"), primary_key=True),
    db.Column("tag_id", db.ForeignKey("tag.id"), primary_key=True),
)


class User(db.Model):
    """사용자(`user`)."""

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    display_name = db.Column("name", db.String(80))
    posts = db.relationship("BlogPost", back_populates="author")


class BlogPost(db.Model):
    """글(`blog_post`)."""

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    author_id = db.Column(db.ForeignKey("user.id"), nullable=False)
    author = db.relationship("User", back_populates="posts")
    tags = db.relationship("Tag", secondary=post_tags, backref="posts")

    def summary(self):
        """제목과 작성자 이름."""
        return f"{self.title} by {self.author.display_name}"


class Tag(db.Model):
    """태그(`tag`)."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(30), unique=True)


class Comment(db.Model):
    """명시 테이블 이름."""

    __tablename__ = "post_comments"

    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.ForeignKey("blog_post.id"))
    body = db.Column(db.Text)
