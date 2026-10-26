import os
import threading
import base64
import mimetypes
import uuid
import time
from telebot.apihelper import ApiTelegramException
from io import BytesIO

import resend
import telebot
from telebot import types
from PIL import Image
from flask import Flask
import psycopg2


# ==============================
# CONFIGURACION
# ==============================

TOKEN = os.environ["BOT_TOKEN"]
PASSWORD = os.environ["BOT_PASSWORD"]
DATABASE_URL = os.environ["DATABASE_URL"]

RESEND_API_KEY = os.environ["RESEND_API_KEY"]
EMAIL_SENDER = os.environ["EMAIL_FROM"]

resend.api_key = RESEND_API_KEY

PRINTER_EMAIL = "yellower@print.brother.com"

MAX_FILES = 10
MAX_TOTAL_SIZE = 20 * 1024 * 1024  # 20 MB

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff",
    ".txt",
    ".doc", ".docx",
    ".xls", ".xlsx",
    ".ppt", ".pptx"
}

CONVERTIBLE_IMAGES = {
    ".jpg", ".jpeg", ".png", ".bmp"
}

# Bloqueo de instancia:
# Se renueva cada 45 segundos y vence a los 180 segundos.
LOCK_ID = 79404057696
LOCK_RENEW_INTERVAL = 45
LOCK_DURATION_SECONDS = 180

INSTANCE_ID = str(uuid.uuid4())
lock_stop_event = threading.Event()
lock_thread = None


# ==============================
# BASE DE DATOS
# ==============================

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():

    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                chat_id BIGINT PRIMARY KEY
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_instance_lock (
                lock_id BIGINT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)

        conn.commit()
        cur.close()

    finally:
        conn.close()


def get_auth_users():

    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute("SELECT chat_id FROM authorized_users")

        rows = cur.fetchall()

        cur.close()

        return set(row[0] for row in rows)

    finally:
        conn.close()


def add_auth_user(chat_id):

    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO authorized_users (chat_id)
            VALUES (%s)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            (chat_id,)
        )

        conn.commit()
        cur.close()

    finally:
        conn.close()


# ==============================
# BLOQUEO DE INSTANCIA
# ==============================

def acquire_bot_lock():

    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO bot_instance_lock (
                lock_id,
                owner_id,
                expires_at
            )
            VALUES (
                %s,
                %s,
                NOW() + (%s * INTERVAL '1 second')
            )
            ON CONFLICT (lock_id)
            DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                expires_at = EXCLUDED.expires_at
            WHERE bot_instance_lock.expires_at < NOW()
            RETURNING owner_id
            """,
            (
                LOCK_ID,
                INSTANCE_ID,
                LOCK_DURATION_SECONDS
            )
        )

        result = cur.fetchone()

        conn.commit()
        cur.close()

        if result is None:
            raise RuntimeError(
                "Otra instancia del bot ya está ejecutándose. "
                "No se iniciará Telegram."
            )

        print("Bloqueo de instancia adquirido correctamente.")

    finally:
        conn.close()


def renew_bot_lock():

    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE bot_instance_lock
            SET expires_at = NOW() + (%s * INTERVAL '1 second')
            WHERE lock_id = %s
            AND owner_id = %s
            RETURNING owner_id
            """,
            (
                LOCK_DURATION_SECONDS,
                LOCK_ID,
                INSTANCE_ID
            )
        )

        result = cur.fetchone()

        conn.commit()
        cur.close()

        return result is not None

    finally:
        conn.close()


def release_bot_lock():

    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            DELETE FROM bot_instance_lock
            WHERE lock_id = %s
            AND owner_id = %s
            """,
            (LOCK_ID, INSTANCE_ID)
        )

        conn.commit()
        cur.close()

        print("Bloqueo de instancia liberado.")

    finally:
        conn.close()


# ==============================
# RESEND
# ==============================

def send_email_resend(
    to,
    subject,
    text,
    attachments=None,
    idempotency_key=None
):

    params = {
        "from": EMAIL_SENDER,
        "to": [to],
        "subject": subject,
        "text": text
    }

    if attachments:

        params["attachments"] = []

        for filename, file_bytes, mime_type in attachments:

            params["attachments"].append({
                "filename": filename,
                "content": base64.b64encode(
                    file_bytes
                ).decode("utf-8"),
                "content_type": mime_type
            })

    try:

        if idempotency_key:

            response = resend.Emails.send(
                params,
                idempotency_key=idempotency_key
            )

        else:

            response = resend.Emails.send(params)

        if isinstance(response, dict):
            email_id = response.get("id")
        else:
            email_id = getattr(response, "id", None)

        if not email_id:
            raise Exception(
                f"Resend no devolvio un ID de envio: {response}"
            )

        return email_id

    except Exception as e:

        raise Exception(
            f"Error de Resend ({type(e).__name__}): {str(e)}"
        )


# ==============================
# FLASK
# ==============================

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot de impresion activo"


def run_server():

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )


# ==============================
# TELEGRAM
# ==============================

bot = telebot.TeleBot(TOKEN)

auth_users = set()
user_data = {}

# Impide que dos callbacks procesen simultaneamente
# la misma tanda.
callback_lock = threading.Lock()


# ==============================
# RENOVACION DEL BLOQUEO
# ==============================

def lock_heartbeat():

    while not lock_stop_event.wait(LOCK_RENEW_INTERVAL):

        try:

            if not renew_bot_lock():

                print(
                    "ERROR: Se perdió el bloqueo de instancia. "
                    "Deteniendo Telegram."
                )

                bot.stop_polling()
                return

            print("Bloqueo de instancia renovado.")

        except Exception as e:

            print(
                "ERROR renovando el bloqueo: "
                f"{type(e).__name__}: {e}"
            )

            bot.stop_polling()
            return


# ==============================
# MENU PRINCIPAL
# ==============================

def show_main_menu(chat_id):

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    markup.row(
        types.KeyboardButton("IMPRIMIR"),
        types.KeyboardButton("GUIA")
    )

    bot.send_message(
        chat_id,
        "Menu principal:",
        reply_markup=markup
    )


# ==============================
# MENU DE IMPRESION
# ==============================

def show_print_menu(chat_id):

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    markup.row(
        types.KeyboardButton("CONFIRMAR IMPRESION"),
        types.KeyboardButton("CANCELAR")
    )

    bot.send_message(
        chat_id,
        "Cuando hayas enviado todos los archivos, "
        "pulsa CONFIRMAR IMPRESION.",
        reply_markup=markup
    )


# ==============================
# GUIA
# ==============================

def show_guide(chat_id):

    guide = (
        "📖 GUIA DE IMPRESION\n\n"

        "📁 ARCHIVOS ADMITIDOS\n\n"

        "DOCUMENTOS:\n"
        "• PDF\n"
        "• TXT\n"
        "• DOC / DOCX\n"
        "• XLS / XLSX\n"
        "• PPT / PPTX\n\n"

        "IMAGENES:\n"
        "• JPG / JPEG\n"
        "• PNG\n"
        "• BMP\n"
        "• GIF\n"
        "• TIF / TIFF\n\n"

        "📦 LIMITES POR ENVIO\n\n"
        "• Maximo 10 archivos por tanda.\n"
        "• Maximo 20 MB en total por correo.\n"
        "• Puedes enviar los archivos uno por uno.\n"
        "• Tambien puedes enviar imagenes como documentos "
        "para conservar su calidad original.\n\n"

        "📄 TAMAÑOS DE ARCHIVO\n\n"
        "• El limite total de la tanda es de 20 MB.\n"
        "• El tamaño de cada archivo debe permitir "
        "respetar ese limite.\n"
        "• Telegram tambien aplica sus propios limites "
        "para la descarga de archivos.\n\n"

        "🖨️ MODOS DE IMPRESION\n\n"

        "• Color.\n"
        "• Blanco y Negro.\n\n"

        "📌 COMO IMPRIMIR\n\n"

        "1. Pulsa IMPRIMIR.\n"
        "2. Envia tus archivos uno por uno.\n"
        "3. Comprueba que todos se hayan recibido.\n"
        "4. Pulsa CONFIRMAR IMPRESION.\n"
        "5. Selecciona Color o Blanco y Negro.\n"
        "6. Espera la confirmacion del envio.\n\n"

        "⚠️ INFORMACION IMPORTANTE\n\n"

        "• Los documentos se envian en su formato original.\n"
        "• En Blanco y Negro se convierten las imagenes "
        "compatibles a escala de grises.\n"
        "• Los PDF y documentos no se modifican.\n"
        "• El tamaño de papel y otras opciones dependen "
        "del documento y de la configuracion de la Brother.\n"
        "• La aceptacion del correo no garantiza que "
        "la impresora haya terminado de imprimir.\n\n"

        "Para comenzar, vuelve al menu y pulsa IMPRIMIR."
    )

    bot.send_message(chat_id, guide)


# ==============================
# AUTENTICACION
# ==============================

@bot.message_handler(commands=["start"])
def send_welcome(message):

    chat_id = message.chat.id

    if chat_id in auth_users:
        show_main_menu(chat_id)

    else:
        bot.send_message(
            chat_id,
            "Ingrese la contraseña:"
        )


@bot.message_handler(
    func=lambda message: message.chat.id not in auth_users
)
def check_auth(message):

    chat_id = message.chat.id

    if (
        message.content_type == "text"
        and message.text == PASSWORD
    ):

        auth_users.add(chat_id)

        add_auth_user(chat_id)

        bot.send_message(
            chat_id,
            "Acceso concedido."
        )

        show_main_menu(chat_id)

    else:

        bot.send_message(
            chat_id,
            "Contraseña incorrecta. Ingrese la contraseña:"
        )


# ==============================
# MENU DE OPCIONES
# ==============================

@bot.message_handler(
    func=lambda message:
        message.content_type == "text"
        and message.text in [
            "IMPRIMIR",
            "GUIA",
            "CONFIRMAR IMPRESION",
            "CANCELAR"
        ]
)
def handle_menu(message):

    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    if message.text == "IMPRIMIR":

        user_data[chat_id] = {
            "files": [],
            "total_size": 0,
            "collecting": True,
            "batch_id": str(uuid.uuid4())
        }

        bot.send_message(
            chat_id,
            "📥 ENVIAR ARCHIVOS\n\n"
            "Puedes enviar hasta 10 archivos "
            "con un maximo de 20 MB en total.\n\n"
            "Envia tus documentos o fotografias "
            "uno por uno."
        )

        show_print_menu(chat_id)

    elif message.text == "GUIA":

        show_guide(chat_id)

    elif message.text == "CONFIRMAR IMPRESION":

        data = user_data.get(chat_id)

        if not data or not data.get("files"):

            bot.send_message(
                chat_id,
                "Todavia no has enviado ningun archivo."
            )

            return

        if not data.get("collecting"):

            bot.send_message(
                chat_id,
                "Ya has confirmado esta tanda."
            )

            return

        data["collecting"] = False

        show_print_mode(chat_id)

    elif message.text == "CANCELAR":

        user_data.pop(chat_id, None)

        bot.send_message(
            chat_id,
            "Impresion cancelada."
        )

        show_main_menu(chat_id)


# ==============================
# MODO DE IMPRESION
# ==============================

def show_print_mode(chat_id):

    markup = types.InlineKeyboardMarkup()

    markup.add(
        types.InlineKeyboardButton(
            "Color",
            callback_data="print_color"
        ),
        types.InlineKeyboardButton(
            "Blanco y Negro",
            callback_data="print_bw"
        )
    )

    bot.send_message(
        chat_id,
        "Seleccione el modo de impresion:",
        reply_markup=markup
    )


# ==============================
# AGREGAR ARCHIVOS A LA TANDA
# ==============================

def add_file_to_batch(chat_id, filename, file_bytes):

    data = user_data.get(chat_id)

    if not data or not data.get("collecting"):

        bot.send_message(
            chat_id,
            "Primero pulsa IMPRIMIR para iniciar una tanda."
        )

        return

    extension = os.path.splitext(filename)[1].lower()

    if extension not in ALLOWED_EXTENSIONS:

        bot.send_message(
            chat_id,
            f"Formato no admitido: {extension or 'desconocido'}"
        )

        return

    if len(data["files"]) >= MAX_FILES:

        bot.send_message(
            chat_id,
            "Has alcanzado el limite de 10 archivos."
        )

        return

    file_size = len(file_bytes)

    if data["total_size"] + file_size > MAX_TOTAL_SIZE:

        bot.send_message(
            chat_id,
            "Este archivo supera el espacio disponible.\n"
            "El maximo total es de 20 MB."
        )

        return

    data["files"].append({
        "filename": filename,
        "bytes": file_bytes,
        "extension": extension
    })

    data["total_size"] += file_size

    total_mb = data["total_size"] / (1024 * 1024)

    bot.send_message(
        chat_id,
        "✅ ARCHIVO RECIBIDO\n\n"
        f"Nombre: {filename}\n"
        f"Archivos: {len(data['files'])}/{MAX_FILES}\n"
        f"Tamano total: {total_mb:.2f} MB / 20 MB"
    )

    if len(data["files"]) >= MAX_FILES:

        bot.send_message(
            chat_id,
            "Has alcanzado los 10 archivos. "
            "Puedes confirmar la impresion."
        )


# ==============================
# RECIBIR FOTOGRAFIAS
# ==============================

@bot.message_handler(content_types=["photo"])
def handle_photo(message):

    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    data = user_data.get(chat_id)

    if not data or not data.get("collecting"):

        bot.send_message(
            chat_id,
            "Primero pulsa IMPRIMIR para iniciar una tanda."
        )

        return

    try:

        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        number = len(
            data.get("files", [])
        ) + 1

        add_file_to_batch(
            chat_id,
            f"fotografia_{number}.jpg",
            downloaded_file
        )

    except Exception as e:

        bot.send_message(
            chat_id,
            f"Error al recibir la fotografia: {str(e)}"
        )


# ==============================
# RECIBIR DOCUMENTOS
# ==============================

@bot.message_handler(content_types=["document"])
def handle_document(message):

    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    data = user_data.get(chat_id)

    if not data or not data.get("collecting"):

        bot.send_message(
            chat_id,
            "Primero pulsa IMPRIMIR para iniciar una tanda."
        )

        return

    try:

        document = message.document

        filename = document.file_name or "documento"

        extension = os.path.splitext(filename)[1].lower()

        if extension not in ALLOWED_EXTENSIONS:

            bot.send_message(
                chat_id,
                "Formato no admitido.\n"
                "Consulta GUIA para ver los formatos disponibles."
            )

            return

        if (
            document.file_size
            and document.file_size > MAX_TOTAL_SIZE
        ):

            bot.send_message(
                chat_id,
                "El archivo supera el limite de 20 MB."
            )

            return

        file_info = bot.get_file(
            document.file_id
        )

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        add_file_to_batch(
            chat_id,
            filename,
            downloaded_file
        )

    except Exception as e:

        bot.send_message(
            chat_id,
            f"Error al recibir el documento: {str(e)}"
        )


# ==============================
# PREPARAR ARCHIVOS
# ==============================

def prepare_attachments(files, is_color):

    attachments = []

    for file in files:

        filename = file["filename"]
        file_bytes = file["bytes"]
        extension = file["extension"]

        if (
            not is_color
            and extension in CONVERTIBLE_IMAGES
        ):

            img = Image.open(
                BytesIO(file_bytes)
            )

            img = img.convert("L")

            output = BytesIO()

            img.save(
                output,
                format="JPEG",
                quality=95
            )

            filename = (
                os.path.splitext(filename)[0] + ".jpg"
            )

            file_bytes = output.getvalue()

        mime_type, _ = mimetypes.guess_type(filename)

        if not mime_type:
            mime_type = "application/octet-stream"

        attachments.append((
            filename,
            file_bytes,
            mime_type
        ))

    return attachments


# ==============================
# ENVIO A IMPRESORA BROTHER
# ==============================

def send_to_printer(chat_id, files, is_color, batch_id):

    try:

        bot.send_message(
            chat_id,
            "Preparando los archivos para la impresora..."
        )

        attachments = prepare_attachments(
            files,
            is_color
        )

        email_id = send_email_resend(
            to=PRINTER_EMAIL,

            # Sin descripcion en el asunto ni en el cuerpo.
            subject=" ",
            text="",

            attachments=attachments,

            # Misma tanda = misma clave de idempotencia.
            idempotency_key=f"telegram-print-{batch_id}"
        )

        bot.send_message(
            chat_id,
            "✅ CORREO ACEPTADO POR RESEND\n\n"
            "Destinatario: impresora Brother\n"
            f"Archivos enviados: {len(attachments)}\n"
            f"ID del correo: {email_id}\n\n"
            "Resend acepto el envio. "
            "La impresion todavia debe ser confirmada "
            "por la impresora."
        )

    except Exception as e:

        bot.send_message(
            chat_id,
            "❌ ERROR AL ENVIAR A LA IMPRESORA\n\n"
            f"Tipo: {type(e).__name__}\n"
            f"Detalle: {str(e)}"
        )


# ==============================
# BOTONES INLINE
# ==============================

@bot.callback_query_handler(
    func=lambda call: True
)
def handle_callbacks(call):

    chat_id = call.message.chat.id

    bot.answer_callback_query(call.id)

    if chat_id not in auth_users:
        return

    if call.data not in ["print_color", "print_bw"]:
        return

    # Solo un callback puede reclamar una tanda a la vez.
    with callback_lock:

        data = user_data.get(chat_id)

        if not data or not data.get("files"):

            bot.send_message(
                chat_id,
                "Esta tanda ya fue procesada o no contiene archivos."
            )

            return

        if data.get("collecting"):

            bot.send_message(
                chat_id,
                "Primero confirma la tanda de impresion."
            )

            return

        if data.get("processing"):

            bot.send_message(
                chat_id,
                "Esta tanda ya se esta procesando."
            )

            return

        # Marcar la tanda como procesada.
        data["processing"] = True

        is_color = call.data == "print_color"

        files = data["files"]
        batch_id = data["batch_id"]

        # Eliminarla para impedir un segundo envío.
        user_data.pop(chat_id, None)

    # Enviar fuera del bloqueo.
    send_to_printer(
        chat_id,
        files,
        is_color,
        batch_id
    )

    show_main_menu(chat_id)


# ==============================
# CORREO DE PRUEBA DESACTIVADO
# ==============================

# El correo de prueba permanece desactivado.
#
# TEST_EMAIL = "pendoo8x@gmail.com"
#
# def send_test_email(chat_id):
#
#     try:
#
#         email_id = send_email_resend(
#             to=TEST_EMAIL,
#             subject="PING",
#             text="ESTE BOT FUNCIONA!"
#         )
#
#         bot.send_message(
#             chat_id,
#             "CORREO ACEPTADO POR RESEND\n\n"
#             f"Destinatario: {TEST_EMAIL}\n"
#             f"ID del correo: {email_id}"
#         )
#
#     except Exception as e:
#
#         bot.send_message(
#             chat_id,
#             "ERROR AL ENVIAR EL CORREO\n\n"
#             f"Detalle: {str(e)}"
#         )


# ==============================
# INICIO
# ==============================

if __name__ == "__main__":

    init_db()

    auth_users = get_auth_users()

    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )

    server_thread.start()

    print("Bot iniciado correctamente. Conectando a Telegram...")

    try:
        while True:
            try:
                bot.infinity_polling(
                    skip_pending=True,
                    timeout=30,
                    long_polling_timeout=30
                )
                break 
            except ApiTelegramException as e:
                if e.error_code == 409:
                    print("Conflicto 409: Instancia vieja aún activa en Render. Reintentando en 10 segundos...")
                    time.sleep(10)
                else:
                    print(f"Error de Telegram: {e}. Reintentando...")
                    time.sleep(5)
            except Exception as e:
                print(f"Error inesperado: {e}. Reintentando en 5 segundos...")
                time.sleep(5)

    except KeyboardInterrupt:
        print("Cierre solicitado.")

    finally:
        print("Apagando bot...")
