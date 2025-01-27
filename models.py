from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class EmailList(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    list_name = db.Column(db.String(100), nullable=False)
    emailcolumn = db.Column(db.String(100), nullable=False)
    emails = db.relationship('Emails', backref='email_list', lazy=True)


class Emails(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email_list_id = db.Column(db.Integer, db.ForeignKey('email_list.id'), nullable=False)
    email = db.Column(db.String(100), nullable=False)
    data = db.Column(db.JSON, nullable=False)  # Contenido completo de la fila
