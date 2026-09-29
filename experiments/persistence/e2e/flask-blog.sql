-- flask fixture DDL recorded by experiments/persistence/build_schema.py (SQLite)
CREATE TABLE blog_post (
	id INTEGER NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	body TEXT, 
	created DATETIME, 
	author_id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(author_id) REFERENCES user (id)
);
CREATE TABLE post_comments (
	id INTEGER NOT NULL, 
	post_id INTEGER, 
	body TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(post_id) REFERENCES blog_post (id)
);
CREATE TABLE post_tags (
	post_id INTEGER NOT NULL, 
	tag_id INTEGER NOT NULL, 
	PRIMARY KEY (post_id, tag_id), 
	FOREIGN KEY(post_id) REFERENCES blog_post (id), 
	FOREIGN KEY(tag_id) REFERENCES tag (id)
);
CREATE TABLE tag (
	id INTEGER NOT NULL, 
	name VARCHAR(30), 
	PRIMARY KEY (id), 
	UNIQUE (name)
);
CREATE TABLE user (
	id INTEGER NOT NULL, 
	email VARCHAR(120) NOT NULL, 
	name VARCHAR(80), 
	PRIMARY KEY (id), 
	UNIQUE (email)
);
