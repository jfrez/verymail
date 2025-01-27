from flask import Flask, request, render_template, jsonify, redirect, url_for
import csv
import os
import json
import time
import boto3
from botocore.exceptions import NoCredentialsError
from validate_email_address import validate_email
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_socketio import SocketIO
from models import db, EmailList, Emails  # Importa tus modelos
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///emails.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Inicializar SQLAlchemy y Flask-Migrate con la aplicación
db.init_app(app)
migrate = Migrate(app, db)


socketio = SocketIO(app)

# Archivo para guardar la caché de dominios
cache_file = "domain_cache.json"
email_cache_file = "email_cache.json"
# Locks para sincronización
domain_cache_lock = threading.Lock()
email_cache_lock = threading.Lock()

# Tiempo máximo de invalidez en segundos (1 año)
CACHE_EXPIRATION = 365 * 24 * 60 * 60

# Variable global para almacenar correos válidos
validated_emails = []



# Crear las tablas en la base de datos
with app.app_context():
    db.create_all()

# Rutas

@app.route('/', methods=['GET'])
def index():
    return render_template('upload.html')

@app.route('/columns', methods=['POST'])
def get_columns():
    if 'file' not in request.files or not request.files['file']:
        return jsonify({"error": "No se seleccionó ningún archivo"}), 400

    file = request.files['file']
    delimiter = request.form.get('delimiter', ',')
    input_file = os.path.join('uploads', file.filename)
    os.makedirs('uploads', exist_ok=True)
    file.save(input_file)

    # Leer las columnas disponibles
    with open(input_file, mode="r", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=delimiter)
        columns = reader.fieldnames

    return jsonify({"columns": columns})

@app.route('/validate', methods=['POST'])
def validate():
    if 'file' not in request.files or not request.files['file']:
        return jsonify({"error": "No se seleccionó ningún archivo"}), 400

    file = request.files['file']
    email_column = request.form.get('email_column')
    delimiter = request.form.get('delimiter', ',')

    if not email_column:
        return jsonify({"error": "El nombre de la columna de correos es obligatorio"}), 400

    input_file = os.path.join('uploads', file.filename)
    os.makedirs('uploads', exist_ok=True)
    file.save(input_file)

    # Llamar a la función de validación en segundo plano
    socketio.start_background_task(validate_emails, input_file, email_column, delimiter)
    return jsonify({"message": "Validación en progreso"})

from concurrent.futures import ThreadPoolExecutor, as_completed

def validate_emails(input_file, email_column, delimiter):
    global validated_emails
    domain_cache = load_domain_cache()
    email_cache = load_email_cache()  # Cargar el caché de correos
    validated_emails = []
    total = 0
    valid_count = 0
    invalid_count = 0
    pending_count = 0

    def validate_email_task(row):
        """
        Función que valida un solo correo y retorna los resultados.
        """
        email = row[email_column].strip()
        if not email:
            return None, None, None  # Saltar filas sin correo
        is_valid, message = verify_email(email)
        return row, is_valid, message

    # Leer los correos desde el archivo
    with open(input_file, mode="r", encoding="utf-8") as file:
        reader = csv.DictReader(file, delimiter=delimiter)
        emails = list(reader)
        total = len(emails)
    # Separar las listas de correos
    emails_in_cache = []
    emails_to_validate = []
    # Separar las listas de correos
    emails_in_cache = []
    emails_to_validate = []

    for row in emails:
        email = row[email_column].strip()
        incache, valid= fastcheck(email)
        if incache:
            emails_in_cache.append((row, valid, "Cache"))
            email = row[email_column].strip()
            validated_emails.append(row)
            valid_count += 1
            socketio.emit('progress', {
                "current": valid_count,
                "total": total,
                "email": email,
                "status": "success",
                "message": "in cache",
                "valid": valid_count,
                "pending": pending_count,
                "invalid": invalid_count
            })
        else:
            emails_to_validate.append(row)
    


    # Emitir progreso para cada correo en caché
    
    
    # Crear un pool de tareas para procesar 10 correos a la vez
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_email = {executor.submit(validate_email_task, row): row for row in emails_to_validate}

        for future in as_completed(future_to_email):
            row = future_to_email[future]
            try:
                result_row, is_valid, message = future.result()

                if result_row:
                    email = result_row[email_column].strip()
                    if is_valid:
                        validated_emails.append(result_row)
                        valid_count += 1
                        status = "success"
                    if is_valid is False:
                        invalid_count += 1
                        status = "error"
                    if is_valid is None:
                        pending_count += 1
                        status = "pending"

                    # Emitir progreso a través de Socket.IO
                    socketio.emit('progress', {
                        "current": valid_count + invalid_count+pending_count,
                        "total": total,
                        "email": email,
                        "status": status,
                        "message": message,
                        "valid": valid_count,
                        "pending": pending_count,
                        "invalid": invalid_count
                    })
                     # Guardar los cambios en ambos cachés
                    save_domain_cache(domain_cache)
                    save_email_cache(email_cache)
            except Exception as e:
                print(f"Error al validar correo: {e}")

    # Guardar los cambios en ambos cachés
    save_domain_cache(domain_cache)
    save_email_cache(email_cache)

    # Emitir evento de finalización
    socketio.emit('validation_complete', {
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "total": total,
        "list": validated_emails
    })


@app.route('/summary', methods=['GET'])
def summary():
    list_id = request.args.get('list_id')

    if not list_id:
        return jsonify({"error": "El ID de la lista es obligatorio"}), 400

    # Buscar la lista en la base de datos
    email_list = EmailList.query.get(list_id)
    if not email_list:
        return jsonify({"error": "La lista especificada no existe"}), 404

    # Obtener los correos asociados a la lista
    emails = Emails.query.filter_by(email_list_id=list_id).all()

    # Convertir correos a una lista de diccionarios para pasarlos al template
    email_data = [{"email": email.email, "data": email.data} for email in emails]

    return render_template(
        'summary.html',
         list_id=list_id,
        list_name=email_list.list_name,
        valid_count=len(email_data),
        emails=email_data
    )

@app.route('/send/<int:list_id>', methods=['GET'])
def send(list_id):
    # Verificar que la lista exista en la base de datos
    email_list = EmailList.query.get(list_id)
    if not email_list:
        return jsonify({"error": "La lista especificada no existe"}), 404

    # Obtener los correos asociados a la lista
    emails = Emails.query.filter_by(email_list_id=list_id).all()
    if not emails:
        return jsonify({"error": "No hay correos asociados a esta lista"}), 404

    # Preparar los datos necesarios para la vista
    email_count = len(emails)  # Número de correos en la lista

    # Renderizar la página de envío con los datos necesarios
    return render_template('send.html', list_id=list_id, email_count=email_count, list_name=email_list.list_name)


import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

@app.route('/sendbackground/<int:list_id>', methods=['POST'])
def sendbackground(list_id):
    # Obtener datos del formulario
    aws_access_key = request.form.get('aws_access_key')
    aws_secret_key = request.form.get('aws_secret_key')
    aws_region = request.form.get('aws_region')
    source_email = request.form.get('source_email')
    subject = request.form.get('subject')
    html_body = request.form.get('html_body')

    if not aws_access_key or not aws_secret_key or not aws_region or not source_email or not subject or not html_body:
        return jsonify({"error": "Todos los campos son obligatorios"}), 400

    # Buscar la lista en la base de datos
    email_list = EmailList.query.get(list_id)
    if not email_list:
        return jsonify({"error": "La lista especificada no existe"}), 404

    # Obtener los correos asociados a la lista
    emails = Emails.query.filter_by(email_list_id=list_id).all()
    if not emails:
        return jsonify({"error": "No hay correos asociados a esta lista"}), 404

    # Configuración del servidor SMTP de SES
    smtp_server = f"email-smtp.{aws_region}.amazonaws.com"
    smtp_port = 587
    smtp_username = aws_access_key
    smtp_password = aws_secret_key

    # Configuración de la conexión SMTP
    context = ssl.create_default_context()

    # Variables para estadísticas
    total_emails = len(emails)
    sent_count = 0
    failed_count = 0
    failed_emails = []

    try:
        # Iniciar conexión SMTP
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)  # Iniciar conexión segura
            server.login(smtp_username, smtp_password)  # Autenticarse con SES

            # Enviar correos
            for i, email_entry in enumerate(emails):
                try:
                    # Crear el correo
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = source_email
                    msg["To"] = email_entry.email

                    # Agregar el contenido HTML
                    part = MIMEText(html_body, "html")
                    msg.attach(part)

                    # Enviar correo
                    server.sendmail(source_email, email_entry.email, msg.as_string())
                    sent_count += 1
                    print(f"Correo enviado a {email_entry.email}.")
                except Exception as e:
                    print(f"Error al enviar correo a {email_entry.email}: {e}")
                    failed_emails.append(email_entry.email)
                    failed_count += 1

                # Emitir estadísticas de envío a través de Socket.IO
                socketio.emit('send_progress', {
                    "current": i + 1,
                    "total": total_emails,
                    "sent": sent_count,
                    "failed": failed_count,
                    "last_email": email_entry.email
                })

        # Emitir evento de finalización
        socketio.emit('send_complete', {
            "total": total_emails,
            "sent": sent_count,
            "failed": failed_count,
            "failed_emails": failed_emails
        })

        # Respuesta final al cliente HTTP
        response_message = {
            "message": "Proceso completado.",
            "total": total_emails,
            "sent": sent_count,
            "failed": failed_count,
            "failed_emails": failed_emails
        }

        if failed_emails:
            response_message["message"] = "Algunos correos no pudieron ser enviados."
            return jsonify(response_message), 206  # Código 206: Partial Content
        else:
            response_message["message"] = "Todos los correos fueron enviados exitosamente."
            return jsonify(response_message), 200

    except smtplib.SMTPAuthenticationError as e:
        return jsonify({"error": f"Error de autenticación SMTP: {e}"}), 401
    except smtplib.SMTPException as e:
        return jsonify({"error": f"Error del servidor SMTP: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error inesperado: {e}"}), 500


@app.route('/add_email', methods=['POST'])
def add_email():
    data = request.json
    if not data or 'email' not in data or 'name' not in data or 'list_name' not in data:
        return jsonify({'message': 'Faltan datos'}), 400

    new_email = EmailList(name=data['name'], email=data['email'], list_name=data['list_name'])
    try:
        db.session.add(new_email)
        db.session.commit()
        return jsonify({'message': 'Correo agregado exitosamente'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'message': 'Error al agregar el correo', 'error': str(e)}), 500

@app.route('/get_emails/<list_name>', methods=['GET'])
def get_emails(list_name):
    emails = EmailList.query.filter_by(list_name=list_name).all()
    email_list = [{'id': email.id, 'name': email.name, 'email': email.email} for email in emails]
    return jsonify(email_list)

@app.route('/update_email/<int:id>', methods=['PUT'])
def update_email(id):
    data = request.json
    email = EmailList.query.get(id)
    if not email:
        return jsonify({'message': 'Correo no encontrado'}), 404

    email.name = data.get('name', email.name)
    email.email = data.get('email', email.email)
    email.list_name = data.get('list_name', email.list_name)

    try:
        db.session.commit()
        return jsonify({'message': 'Correo actualizado exitosamente'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'message': 'Error al actualizar el correo', 'error': str(e)}), 500

@app.route('/delete_email/<int:id>', methods=['DELETE'])
def delete_email(id):
    email = EmailList.query.get(id)
    if not email:
        return jsonify({'message': 'Correo no encontrado'}), 404

    try:
        db.session.delete(email)
        db.session.commit()
        return jsonify({'message': 'Correo eliminado exitosamente'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'message': 'Error al eliminar el correo', 'error': str(e)}), 500

@app.route('/get_lists', methods=['GET'])
def get_lists():
    # Obtener todas las listas con sus IDs y nombres
    lists = EmailList.query.with_entities(EmailList.id, EmailList.list_name).all()
    list_data = [{"id": l[0], "name": l[1]} for l in lists]
    return jsonify(list_data)

@app.route('/save_validated_list', methods=['POST'])
def save_validated_list():
    import re

    def clean_row(row):
        """
        Limpia las claves y valores de un diccionario.
        Realiza las siguientes acciones:
        - Elimina caracteres no deseados como '\ufeff'.
        - Normaliza las claves a minúsculas y elimina espacios adicionales.
        - Quita espacios en los valores y valida correos electrónicos.
        - Normaliza correos electrónicos a minúsculas.
        """
        cleaned_row = {}
        for key, value in row.items():
            # Limpiar clave
            cleaned_key = key.strip().replace('\ufeff', '').lower()

            # Limpiar valor
            if isinstance(value, str):
                cleaned_value = value.strip().replace('\ufeff', '')
                if cleaned_key == "correo":  # Normalizar correos electrónicos
                    cleaned_value = cleaned_value.lower()
                    if not re.match(r"[^@]+@[^@]+\.[^@]+", cleaned_value):  # Validar formato de correo
                        cleaned_value = None  # Correo inválido
            else:
                cleaned_value = value

            cleaned_row[cleaned_key] = cleaned_value
        return cleaned_row

    data = request.json
    list_id = data.get('list_id')  # ID de la lista existente (puede ser None)
    list_name = data.get('list_name')  # Nombre de la lista nueva, si es necesario
    emails = data.get('emails', [])
    email_column = data.get('emailcolumn')  # Nombre de la columna de correos

    if list_id == None:  # Si no hay un ID, crear una nueva lista
        if not list_name:
            return jsonify({'error': 'Debe proporcionarse un nombre para la nueva lista'}), 400

        email_list = EmailList(list_name=list_name, emailcolumn=email_column)
        db.session.add(email_list)
        db.session.flush()  # Obtener el ID de la nueva lista
        list_id = email_list.id
    else:
        # Buscar la lista por su ID
        email_list = EmailList.query.get(list_id)
        if not email_list:
            return jsonify({'error': 'La lista especificada no existe'}), 404

    try:
        # Limpieza de los datos de entrada
        cleaned_emails = [clean_row(row) for row in emails]

        # Filtrar solo los correos validados
        valid_emails = [
            row for row in cleaned_emails
            if row.get(email_column.lower())  # Verifica que el correo exista después de limpiar
        ]

        # Eliminar los correos existentes asociados a la lista antes de guardar los nuevos
        Emails.query.filter_by(email_list_id=list_id).delete()

        # Crear registros en Emails con los nuevos datos
        for row in valid_emails:
            email_value = row.get(email_column.lower())
            if email_value:  # Solo procesar filas válidas con un correo
                # Asegurarse de incluir todas las columnas originales en `data`
                email_entry = Emails(
                    email_list_id=list_id,
                    email=email_value,
                    data=row  # Almacenar la fila completa como JSON
                )
                db.session.add(email_entry)

        db.session.commit()
        return jsonify({
            'message': f'Lista \"{email_list.list_name}\" guardada exitosamente con {len(valid_emails)} correos válidos.',
            'list_id': list_id
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/validate_existing', methods=['GET'])
def validate_existing():
    list_id = request.args.get('list_id')  # Ahora se usa list_id en lugar de list_name

    if not list_id:
        return jsonify({"error": "El ID de la lista es obligatorio"}), 400

    # Buscar la lista en la base de datos por su ID
    email_list = EmailList.query.get(list_id)

    if not email_list:
        return jsonify({"error": "La lista seleccionada no existe"}), 404

    # Obtener los correos asociados a la lista
    emails = Emails.query.filter_by(email_list_id=email_list.id).all()

    if not emails:
        return jsonify({"error": "No hay correos asociados a la lista seleccionada"}), 404

    # Validar los correos de la lista existente
    domain_cache = load_domain_cache()
    validated_emails = []
    valid_count = 0
    invalid_count = 0
    pending_count = 0
    for i, email_entry in enumerate(emails):
        email = email_entry.email
        is_valid, message = verify_email(email)

        if is_valid is True:
            validated_emails.append({
                "email": email,
                "status": "valid",
                "message": message,
                "data": email_entry.data
            })
            valid_count += 1
        if is_valid is False:
            validated_emails.append({
                "email": email,
                "status": "invalid",
                "message": message,
                "data": email_entry.data
            })
            invalid_count += 1

        if is_valid is None:
            validated_emails.append({
                "email": email,
                "status": "pending",
                "message": message,
                "data": email_entry.data
            })
            pending_count += 1

        # Emitir progreso con Socket.IO
        socketio.emit('progress', {
            "current": i + 1,
            "total": len(emails),
            "email": email,
            "status": "success" if is_valid else "error",
            "message": message,
            "valid": valid_count,
            "invalid": invalid_count,
            "pending": pending_count
        })

        save_domain_cache(domain_cache)

    # Actualizar la lista validada en la base de datos
    try:
        # Eliminar correos existentes
        Emails.query.filter_by(email_list_id=email_list.id).delete()

        # Insertar los nuevos correos validados
        for validated_email in validated_emails:
            email_entry = Emails(
                email_list_id=email_list.id,
                email=validated_email["email"],
                data=validated_email["data"]  # Mantener la fila completa
            )
            db.session.add(email_entry)

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al actualizar la lista: {str(e)}"}), 500

    # Emitir evento de finalización
    socketio.emit('validation_complete', {
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "pending_count": pending_count,
        "total": len(emails),
        "list": validated_emails
    })

    return jsonify({
        "message": "Validación completada y lista actualizada exitosamente",
        "valid_count": valid_count,
        "invalid_count": invalid_count,
        "total": len(emails)
    })


import redis

# Initialize Redis client
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0, decode_responses=True)

CACHE_EXPIRATION = 3600  # Cache expiration time in seconds

def load_domain_cache():
    """
    Carga el caché de dominios desde Redis.
    """
    keys = redis_client.keys('domain_cache:*')
    domain_cache = {}
    for key in keys:
        domain = key.split(':', 1)[1]
        domain_cache[domain] = json.loads(redis_client.get(key))
    return domain_cache

def save_domain_cache(domain_cache):
    """
    Guarda el caché de dominios en Redis.
    """
    for domain, data in domain_cache.items():
        redis_client.setex(f'domain_cache:{domain}', CACHE_EXPIRATION, json.dumps(data))

def load_email_cache():
    """
    Carga el caché de correos electrónicos desde Redis.
    """
    keys = redis_client.keys('email_cache:*')
    email_cache = {}
    for key in keys:
        email = key.split(':', 1)[1]
        email_cache[email] = json.loads(redis_client.get(key))
    return email_cache

def save_email_cache(email_cache):
    """
    Guarda el caché de correos electrónicos en Redis.
    """
    for email, data in email_cache.items():
        redis_client.setex(f'email_cache:{email}', CACHE_EXPIRATION, json.dumps(data))

def dump_cache_to_file(file_path):
    """
    Volcar los datos de caché en Redis a un archivo JSON.
    """
    try:
        domain_cache = load_domain_cache()
        email_cache = load_email_cache()

        dump_data = {
            "domain_cache": domain_cache,
            "email_cache": email_cache
        }

        with open(file_path, 'w', encoding='utf-8') as file:
            json.dump(dump_data, file, ensure_ascii=False, indent=4)

        print(f"Cache successfully dumped to {file_path}")
    except Exception as e:
        print(f"Error dumping cache to file: {e}")


import socket
import smtplib
import time
import dns.resolver
domain_cache=load_domain_cache()
email_cache=load_email_cache()
CACHE_EXPIRATION = 3600*24*265  # Expiración de 1 hora para el caché
def fastcheck(email):

    # Verificar formato del correo
    if not validate_email(email):
        return False, "Formato inválido"

    # Extraer el dominio del correo
    domain = email.split('@')[-1]
    current_time = time.time()

    # Verificar si el correo está en el caché
    if email in email_cache:
        cached_entry = email_cache[email]
        if current_time - cached_entry["timestamp"] < CACHE_EXPIRATION:
            return True,cached_entry["valid"]
    if domain in domain_cache:
        cached_entry = domain_cache[domain]
        if current_time - cached_entry["timestamp"] < CACHE_EXPIRATION:
            if not cached_entry["valid"]:
                return True, False
    return False,False
            
def verify_email(email):
    """
    Verifica si un correo electrónico es válido siguiendo estos pasos:
    1. Verifica el formato del correo.
    2. Verifica si el correo está en el caché.
    3. Verifica si el dominio existe y tiene registros MX.
    4. Intenta conectarse al servidor de correo para verificar la existencia del usuario.
    """
    domain_cache=load_domain_cache()
    email_cache=load_email_cache()

    # Verificar formato del correo
    if not validate_email(email):
        return False, "Formato inválido"

    # Extraer el dominio del correo
    domain = email.split('@')[-1]
    current_time = time.time()
            

    try:
        # Paso 1: Verificar si el dominio existe
        try:
            socket.gethostbyname(domain)
        except socket.gaierror:
            domain_cache[domain] = {"valid": False, "timestamp": current_time}
            save_domain_cache(domain_cache)
            return False, "El dominio no existe"

        # Paso 2: Verificar si el dominio tiene registros MX
        try:
            mx_records = dns.resolver.resolve(domain, 'MX')
            if not mx_records:
                domain_cache[domain] = {"valid": False, "timestamp": current_time}
                save_domain_cache(domain_cache)
                return False, "Dominio sin registros MX"
            else:
                domain_cache[domain] = {"valid": True, "timestamp": current_time}
                save_domain_cache(domain_cache)

        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            domain_cache[domain] = {"valid": False, "timestamp": current_time}
            save_domain_cache(domain_cache)
            return False, "Dominio sin registros MX"
        except Exception as e:
            domain_cache[domain] = {"valid": False, "timestamp": current_time}
            save_domain_cache(domain_cache)
            return False, f"Error al resolver registros MX: {e}"

        # Paso 3: Verificar la existencia del usuario
        smtp_server = str(mx_records[0].exchange).strip('.')
        try:
            server = smtplib.SMTP(smtp_server, 25, timeout=10)
            server.helo()  # Saludo inicial al servidor
            server.mail('test@example.com')  # Correo remitente ficticio
            code, response = server.rcpt(email)  # Verificar el receptor
            server.quit()

            response_str = response.decode('utf-8') if isinstance(response, bytes) else response

            if code == 250 or "5.4.1" in response_str or "5.7.606" in response_str:
                email_cache[email] = {"valid": True, "timestamp": current_time}
                domain_cache[domain] = {"valid": True, "timestamp": current_time}
                save_email_cache(email_cache)
                return True, f"Correo válido. Respuesta del servidor: {response_str}"
            elif code == 450 or code == 421:  # Códigos de bloqueo temporal
                email_cache[email] = {"valid": None, "timestamp": current_time}  # Estado "pending"
                save_email_cache(email_cache)
                return None, f"Correo en estado 'pending'. Respuesta del servidor: {response_str}"
            else:
                email_cache[email] = {"valid": False, "timestamp": current_time}
                save_email_cache(email_cache)
                return False, f"El servidor respondió: {response_str}"
        except Exception as e:
            email_cache[email] = {"valid": None, "timestamp": current_time}  # Estado "pending"
            save_email_cache(email_cache)
            return None, f"Error al conectar con el servidor SMTP: {e}"

    except Exception as e:
        print(f"Error al verificar el dominio {domain}: {e}")
        email_cache[email] = {"valid": None, "timestamp": current_time}  # Estado "pending"
        save_email_cache(email_cache)
        return None, f"Error inesperado: {e}"


if __name__ == "__main__":
    socketio.run(app, debug=True)
