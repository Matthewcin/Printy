
import os
import threading
import base64
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

# Imagenes que podemos convertir a blanco y negro
CONVERTIBLE_IMAGES = {
    ".jpg", ".jpeg", ".png", ".bmp"
}


# ==============================
# RESEND
# ==============================

def send_email_resend(to, subject, text, attachments=None):

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
                ).decode("utf-8")
            })

    try:
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
# BASE DE DATOS
# ==============================

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS authorized_users (
            chat_id BIGINT PRIMARY KEY
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_auth_users():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT chat_id FROM authorized_users")
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return set(row[0] for row in rows)


def add_auth_user(chat_id):
    conn = psycopg2.connect(DATABASE_URL)
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
    conn.close()


# ==============================
# FLASK
# ==============================

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot de impresion activo"


def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ==============================
# TELEGRAM
# ==============================

bot = telebot.TeleBot(TOKEN)

auth_users = set()
user_data = {}


# ==============================
# TECLADOS
# ==============================

def show_main_menu(chat_id):

    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True
    )

    markup.add(
        types.KeyboardButton("IMPRIMIR")
    )

    bot.send_message(
        chat_id,
        "Menu principal:",
        reply_markup=markup
    )


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
# MENU PRINCIPAL
# ==============================

@bot.message_handler(
    func=lambda message:
        message.content_type == "text"
        and message.text in [
            "IMPRIMIR",
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
            "collecting": True
        }

        bot.send_message(
            chat_id,
            "ENVIAR ARCHIVOS\n\n"
            "Puedes enviar fotografias y documentos.\n"
            "Maximo: 10 archivos y 20 MB en total.\n\n"
            "Formatos admitidos:\n"
            "PDF, JPG, PNG, BMP, GIF, TIFF, TXT, "
            "DOC, DOCX, XLS, XLSX, PPT y PPTX.\n\n"
            "Envia los archivos uno por uno."
        )

        show_print_menu(chat_id)

    elif message.text == "CONFIRMAR IMPRESION":

        data = user_data.get(chat_id)

        if not data or not data.get("files"):
            bot.send_message(
                chat_id,
                "Todavia no has enviado ningun archivo."
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
            "Este archivo supera el limite disponible.\n"
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
        "ARCHIVO RECIBIDO\n\n"
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

    try:
        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        if file_info.file_size and file_info.file_size > MAX_TOTAL_SIZE:
            bot.send_message(
                chat_id,
                "La fotografia supera el limite permitido."
            )
            return

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        number = len(
            user_data.get(chat_id, {}).get("files", [])
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

    try:
        document = message.document

        filename = document.file_name or "documento"

        extension = os.path.splitext(filename)[1].lower()

        if extension not in ALLOWED_EXTENSIONS:
            bot.send_message(
                chat_id,
                "Formato no admitido.\n"
                "Envia PDF, imagenes u otro documento imprimible."
            )
            return

        if document.file_size and document.file_size > MAX_TOTAL_SIZE:
            bot.send_message(
                chat_id,
                "El archivo supera el limite de 20 MB."
            )
            return

        file_info = bot.get_file(document.file_id)

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

        # Convertimos imagenes compatibles a blanco y negro
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

            filename = os.path.splitext(filename)[0] + ".jpg"
            file_bytes = output.getvalue()

        attachments.append((
            filename,
            file_bytes,
            "application/octet-stream"
        ))

    return attachments


# ==============================
# ENVIO A IMPRESORA BROTHER
# ==============================

def send_to_printer(chat_id, files, is_color):

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
            subject="Impresion desde Telegram",
            text=(
                "Documentos enviados desde el bot de Telegram.\n"
                f"Cantidad de archivos: {len(attachments)}"
            ),
            attachments=attachments
        )

        bot.send_message(
            chat_id,
            "CORREO ACEPTADO POR RESEND\n\n"
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
            "ERROR AL ENVIAR A LA IMPRESORA\n\n"
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

    if call.data in ["print_color", "print_bw"]:

        data = user_data.get(chat_id)

        if not data or not data.get("files"):

            bot.send_message(
                chat_id,
                "No se encontraron archivos. "
                "Inicia una nueva impresion."
            )

            show_main_menu(chat_id)
            return

        is_color = call.data == "print_color"

        files = data["files"]

        # Liberamos los archivos de la sesion
        user_data.pop(chat_id, None)

        send_to_printer(
            chat_id,
            files,
            is_color
        )

        show_main_menu(chat_id)


# ==============================
# CORREO DE PRUEBA DESACTIVADO
# ==============================

# De momento se desactiva el correo de prueba.
# Para volver a utilizarlo, descomentar esta seccion
# y agregar nuevamente el boton TEST EMAIL al menu.
#
# TEST_EMAIL = "pendoo8x@gmail.com"
#
# def send_test_email(chat_id):
#     try:
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

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
