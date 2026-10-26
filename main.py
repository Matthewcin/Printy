
import os
import threading
import base64
from io import BytesIO

import requests
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

RESEND_URL = "https://api.resend.com/emails"

PRINTER_EMAIL = "79404057696@print.brother.com"
TEST_EMAIL = "pendoo8x@gmail.com"


# ==============================
# RESEND
# ==============================

class ResendAPIError(Exception):
    pass


def send_email_resend(to, subject, text, attachment=None):
    """
    Envia un correo mediante la API HTTPS de Resend.
    attachment: tupla (nombre_archivo, bytes, tipo_mime)
    """

    payload = {
        "from": EMAIL_SENDER,
        "to": [to],
        "subject": subject,
        "text": text
    }

    if attachment:
        filename, file_bytes, mime_type = attachment

        payload["attachments"] = [{
            "filename": filename,
            "content": base64.b64encode(file_bytes).decode("utf-8")
        }]

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            RESEND_URL,
            headers=headers,
            json=payload,
            timeout=30
        )

    except requests.Timeout:
        raise ResendAPIError(
            "Tiempo de espera agotado al conectar con Resend."
        )

    except requests.RequestException as e:
        raise ResendAPIError(
            f"Error de conexion con Resend: {type(e).__name__}: {e}"
        )

    try:
        result = response.json()
    except ValueError:
        result = {"message": response.text[:1000]}

    if not response.ok:
        raise ResendAPIError(
            f"Resend rechazo el envio.\n"
            f"HTTP: {response.status_code}\n"
            f"Respuesta: {result}"
        )

    email_id = result.get("id")

    if not email_id:
        raise ResendAPIError(
            f"Resend respondio sin un ID de correo: {result}"
        )

    return email_id


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
# MENU PRINCIPAL
# ==============================

def show_main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add(
        types.KeyboardButton("IMPRIMIR"),
        types.KeyboardButton("TEST EMAIL")
    )

    bot.send_message(
        chat_id,
        "Menu principal:",
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
        bot.send_message(chat_id, "Ingrese la contraseña:")


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

        bot.send_message(chat_id, "Acceso concedido.")
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
        and message.text in ["IMPRIMIR", "TEST EMAIL"]
)
def handle_menu(message):
    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    if message.text == "IMPRIMIR":
        bot.send_message(
            chat_id,
            "Envia la foto a imprimir."
        )

    elif message.text == "TEST EMAIL":
        bot.send_message(
            chat_id,
            "Enviando correo de prueba mediante Resend..."
        )
        send_test_email(chat_id)


# ==============================
# CORREO DE PRUEBA
# ==============================

def send_test_email(chat_id):
    try:
        email_id = send_email_resend(
            to=TEST_EMAIL,
            subject="PING",
            text="ESTE BOT FUNCIONA!"
        )

        bot.send_message(
            chat_id,
            "CORREO ACEPTADO POR RESEND\n\n"
            f"Destinatario: {TEST_EMAIL}\n"
            "Asunto: PING\n"
            "Mensaje: ESTE BOT FUNCIONA!\n\n"
            f"ID del correo: {email_id}\n\n"
            "Revisa la bandeja de entrada y spam."
        )

    except ResendAPIError as e:
        bot.send_message(
            chat_id,
            "ERROR AL ENVIAR EL CORREO\n\n"
            f"{str(e)}"
        )

    except Exception as e:
        bot.send_message(
            chat_id,
            "ERROR INESPERADO\n\n"
            f"Tipo: {type(e).__name__}\n"
            f"Detalle: {str(e)}"
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
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        user_data[chat_id] = {
            "photo": downloaded_file
        }

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

    except Exception as e:
        bot.send_message(
            chat_id,
            f"Error al recibir la foto: {e}"
        )


# ==============================
# BOTONES INLINE
# ==============================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id

    bot.answer_callback_query(call.id)

    if chat_id not in auth_users:
        return

    if call.data in ["print_color", "print_bw"]:

        is_color = call.data == "print_color"

        photo_bytes = user_data.get(
            chat_id, {}
        ).get("photo")

        if photo_bytes:
            bot.send_message(
                chat_id,
                "Procesando imagen..."
            )

            send_to_printer(
                chat_id,
                photo_bytes,
                is_color
            )

            user_data.pop(chat_id, None)

        else:
            bot.send_message(
                chat_id,
                "Error: No se encontro la foto. Vuelve a enviarla."
            )

        show_main_menu(chat_id)


# ==============================
# ENVIO A IMPRESORA BROTHER
# ==============================

def send_to_printer(chat_id, photo_bytes, is_color):
    try:
        bot.send_message(
            chat_id,
            "Enviando imagen a la impresora mediante Resend..."
        )

        img = Image.open(BytesIO(photo_bytes))

        if not is_color:
            img = img.convert("L")
        else:
            img = img.convert("RGB")

        img = img.resize(
            (1240, 1754),
            Image.Resampling.LANCZOS
        )

        output = BytesIO()

        img.save(
            output,
            format="JPEG",
            quality=95
        )

        image_data = output.getvalue()

        email_id = send_email_resend(
            to=PRINTER_EMAIL,
            subject="Impresion desde Telegram",
            text="Documento enviado desde el bot de Telegram.",
            attachment=(
                "impresion.jpg",
                image_data,
                "image/jpeg"
            )
        )

        bot.send_message(
            chat_id,
            "CORREO ACEPTADO POR RESEND\n\n"
            "Destinatario: impresora Brother\n"
            f"ID del correo: {email_id}\n\n"
            "Resend acepto el envio. Esto no confirma "
            "todavia que la impresora haya terminado de imprimir."
        )

    except ResendAPIError as e:
        bot.send_message(
            chat_id,
            "ERROR DE RESEND AL ENVIAR A LA IMPRESORA\n\n"
            f"{str(e)}"
        )

    except Exception as e:
        bot.send_message(
            chat_id,
            "ERROR AL PROCESAR O ENVIAR LA IMAGEN\n\n"
            f"Tipo: {type(e).__name__}\n"
            f"Detalle: {str(e)}"
        )


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
