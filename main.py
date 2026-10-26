import os
import threading
import smtplib
from email.message import EmailMessage
from io import BytesIO
import telebot
from telebot import types
from PIL import Image
from flask import Flask
import psycopg2

TOKEN = "8837741358:AAFckJCvuACBT4-KIyNp7WqxQ69YEQEYSXY"
PASSWORD = "160620"
PRINTER_EMAIL = "yellower@print.brother.com"
EMAIL_SENDER = "printythebot@outlook.com"
EMAIL_PASSWORD = "lybjezesstkmwmzb"
SMTP_SERVER = "smtp-mail.outlook.com"
SMTP_PORT = 587
DATABASE_URL = "postgresql://neondb_owner:npg_VvNCRHKr2L4I@ep-quiet-sea-b40g1xzz-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS authorized_users (chat_id BIGINT PRIMARY KEY)")
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
    cur.execute("INSERT INTO authorized_users (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING", (chat_id,))
    conn.commit()
    cur.close()
    conn.close()

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de impresion activo"

def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

bot = telebot.TeleBot(TOKEN)
auth_users = set()
user_states = {}
user_data = {}

@bot.message_handler(commands=["start"])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id in auth_users:
        show_main_menu(chat_id)
    else:
        user_states[chat_id] = "WAITING_PASSWORD"
        bot.send_message(chat_id, "Ingrese la contraseña:")

@bot.message_handler(func=lambda message: message.chat.id not in auth_users)
def check_auth(message):
    chat_id = message.chat.id
    if message.text == PASSWORD:
        auth_users.add(chat_id)
        add_auth_user(chat_id)
        user_states[chat_id] = "MAIN_MENU"
        bot.send_message(chat_id, "Acceso concedido.")
        show_main_menu(chat_id)
    else:
        user_states[chat_id] = "WAITING_PASSWORD"
        bot.send_message(chat_id, "Sesión reiniciada o contraseña incorrecta. Ingrese la contraseña:")

@bot.message_handler(func=lambda message: message.text in ["IMPRIMIR", "INFORMACION", "CONFIGURACION"])
def handle_menu(message):
    chat_id = message.chat.id
    if message.text == "IMPRIMIR":
        user_states[chat_id] = "WAITING_PHOTO"
        bot.send_message(chat_id, "Envía la foto a imprimir.")
    elif message.text == "INFORMACION":
        get_printer_info(chat_id)
    elif message.text == "CONFIGURACION":
        show_config_menu(chat_id)

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id
    if user_states.get(chat_id) == "WAITING_PHOTO":
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        user_data[chat_id] = {"photo": downloaded_file}
        
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Color", callback_data="print_color"), types.InlineKeyboardButton("Blanco y Negro", callback_data="print_bw"))
        bot.send_message(chat_id, "Seleccione el modo de impresión:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    
    if call.data in ["print_color", "print_bw"]:
        is_color = call.data == "print_color"
        photo_bytes = user_data.get(chat_id, {}).get("photo")
        
        if photo_bytes:
            bot.send_message(chat_id, "Procesando imagen...")
            send_to_printer(chat_id, photo_bytes, is_color)
        else:
            bot.send_message(chat_id, "Error: No se encontró la foto.")
        
        user_states[chat_id] = "MAIN_MENU"
        show_main_menu(chat_id)
    elif call.data.startswith("cfg_"):
        bot.send_message(chat_id, "Función de configuración en espera del modelo exacto de la impresora.")

def send_to_printer(chat_id, photo_bytes, is_color):
    try:
        bot.send_message(chat_id, "Enviando mensaje a printy...")
        
        img = Image.open(BytesIO(photo_bytes))
        if not is_color:
            img = img.convert("L")
        else:
            img = img.convert("RGB")
        
        img = img.resize((2480, 3508), Image.Resampling.LANCZOS)
        
        output = BytesIO()
        img.save(output, format="JPEG", quality=95)
        image_data = output.getvalue()
        
        msg = EmailMessage()
        msg['Subject'] = 'Impresion desde Telegram'
        msg['From'] = EMAIL_SENDER
        msg['To'] = PRINTER_EMAIL
        msg.set_content("Documento enviado desde el bot de Telegram.")
        
        msg.add_attachment(image_data, maintype='image', subtype='jpeg', filename='impresion.jpg')
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
            
        bot.send_message(chat_id, "Estado de envio: ¡Exitoso! Se imprimió correctamente.")
    except Exception as e:
        bot.send_message(chat_id, f"Error en el estado de envio: {str(e)}")

def get_printer_info(chat_id):
    bot.send_message(chat_id, "Estado: Conectada\nUptime: Calculando...\nTinta Cyan: ?\nTinta Magenta: ?\nTinta Yellow: ?\nTinta Black: ?\n\nEsperando modelo Brother para implementar lectura SNMP.")

def show_config_menu(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Modo Ahorro", callback_data="cfg_ahorro"), types.InlineKeyboardButton("Limpieza de Cabezales", callback_data="cfg_limpieza"))
    bot.send_message(chat_id, "Opciones de configuración (Esperando modelo):", reply_markup=markup)

if __name__ == "__main__":
    init_db()
    auth_users = get_auth_users()
    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    bot.polling(non_stop=True)
