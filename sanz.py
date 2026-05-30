import telebot
from telebot import types
import re
from collections import defaultdict
import csv
import os
import io
import sqlite3
import math
import unicodedata
import os, time, json
import time, threading, requests
import uuid
import qrcode
from datetime import datetime, timedelta

# Fungsi untuk format angka tanpa .0 jika bulat
def fmt_num(x):
    try:
        x = float(x)
    except:
        return str(x)
    return str(int(x)) if x.is_integer() else str(x)

TOKEN = "8429453994:AAGslIwjWhWs-N3CKnfYChL0zhLpI19X_tM"
bot = telebot.TeleBot(TOKEN)
OWNER_USER_ID = 7230983425  # Ganti dengan user_id asli @real

import json
import os

filter_file = "group_filters.json"

# Struktur: { "chat_id": ["kata1", "kata2"] }
group_filters = {}

def load_group_filters():
    global group_filters
    try:
        if os.path.exists(filter_file):
            with open(filter_file, "r") as f:
                group_filters = json.load(f)
        else:
            group_filters = {}
    except:
        group_filters = {}

def save_group_filters():
    with open(filter_file, "w") as f:
        json.dump(group_filters, f, indent=2)

load_group_filters()

# ----- 
def command_case_insensitive(command_list):
    result = []
    for cmd in command_list:
        result.append(cmd.lower())
        result.append(cmd.upper())
        result.append(cmd.capitalize())
    return list(set(result))

user_data = set()
group_data = set()

def save_user(uid):
    user_data.add(uid)
    

# ==============================
#   DATABASE ALLOWED GROUPS
# ==============================
DB_FILE = "allowed_groups.db"
allowed_groups = set()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS allowed_groups (chat_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

def load_allowed_groups():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id FROM allowed_groups")
    rows = c.fetchall()
    for r in rows:
        allowed_groups.add(int(r[0]))   # pastikan int
    conn.close()

def save_group(chat_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO allowed_groups (chat_id) VALUES (?)", (int(chat_id),))
    conn.commit()
    conn.close()

# panggil saat bot mulai
init_db()
load_allowed_groups()

# ==============================
#   DECORATOR VALIDASI
# ==============================
def is_sewa_aktif(chat_id: str) -> bool:
    """Cek apakah grup punya sewa aktif"""
    if chat_id not in sewabot_map:
        return False
    
    data = sewabot_map[chat_id]
    if data["paket"] == "permanen":
        return True
    
    # kalau paket 1 bulan, cek expired
    if data["paket"] == "1bulan" and data["expire"]:
        return data["expire"] > int(time.time())
    
    return False
    
# ==============================
#   PERBAIKI VALIDASI SEWA
# ==============================
def require_host_or_admin(func):
    """Decorator untuk cek sewa aktif + admin/owner"""
    def wrapper(message, *args, **kwargs):
        chat_id = str(message.chat.id)

        # cek sewa aktif ATAU grup di daftar allowed
        if (int(chat_id) not in allowed_groups) and (not is_sewa_aktif(chat_id)):
            bot.reply_to(
                message,
                "Grup ini belum aktif sewa bot.\n"
                "Silakan aktifkan dengan /sewabot"
            )
            return

        # kalau private chat → harus owner
        if message.chat.type == "private":
            if not is_owner(message.from_user.id):
                bot.reply_to(message, "Hanya owner yang bisa memakai perintah ini.")
                return
        else:
            try:
                member = bot.get_chat_member(message.chat.id, message.from_user.id)
                if not (member.status in ["administrator", "creator"] or is_owner(message.from_user.id)):
                    bot.reply_to(message, "Hanya admin grup yang bisa memakai perintah ini.")
                    return
            except:
                bot.reply_to(message, "Tidak bisa mengecek status admin.")
                return

        return func(message, *args, **kwargs)
    return wrapper

# ---- data utama ----
rekap_data = {}  # sementara selama proses rekap (game sedang berjalan)

game_data = defaultdict(lambda: {
    "games": [],
    "dev": "",
    "rol": "",
    "last_win": "",
    "saldo": defaultdict(int),
    "history": []
})

message_to_delete = defaultdict(dict)
pinned_message_id = defaultdict(int)

# simpan riwayat pesan pin & mapping pesan -> game_id
pinned_history = defaultdict(list)
message_game_map = defaultdict(dict)

# ---- mode reset (harus di atas handler supaya tidak NameError) ----
reset_mode_active = {}  # key: chat_id -> bool → apakah mode reset aktif
reset_mode_timer = {}   # key: chat_id -> threading.Timer → otomatis nonaktif setelah 10 menit

# ---- helper parsing / perhitungan (pakai yg asli) ----
def extract_numbers(text):
    return [int(re.findall(r'\d+', x)[0]) for x in text.split() if re.findall(r'\d+', x)]

def clean_number(num):
    return int(re.findall(r'\d+', num)[0])

# ---- fitur proteksi khusus /rekapwin ----
rekapwin_owner = {}  # key: chat_id -> user_id
current_game_host = {}  # key: chat_id -> user_id

def check_rekap_owner(call):
    chat_id = call.message.chat.id
    owner = rekapwin_owner.get(chat_id)
    if owner is None:
        return True
    if call.from_user.id != owner:
        bot.answer_callback_query(
            call.id,
            "Maaf hanya user yang menjalankan perintah ini yang bisa menekan tombol.",
            show_alert=True
        )
        return False
    return True

# ---- proses saldo ----
def _bucket_size(effective_percent: float) -> int:
    p = effective_percent
    if p <= 5.5:
        return 100
    elif p <= 6.5:
        return 90
    elif p <= 7.5:
        return 8
    elif p <= 8.5:
        return 7
    else:
        return 6  # persen > 8.5

def calculate_fee(n: int, fee_percent: float = 6, is_double: bool = False) -> int:
    if n <= 0:
        return 0

    # mapping fee -> interval size
    interval_map = {
        5: 10,
        6: 9,
        7: 8,
        8: 7,
        9: 6,
        10: 5
    }

    interval = interval_map.get(int(fee_percent), 10)  # default interval 10

    # range pertama 1-9 selalu fee 1
    if n <= 9:
        return 1

    # bucket berikutnya sesuai interval
    return 1 + ((n - 10) // interval) + 1

def proses_saldo(pemain_list, sisi_menang, sisi_kalah, menang=True, fee_percent=6, lw_saldo=None):
    saldo = defaultdict(int)

    # masukkan saldo LW kalau ada
    if lw_saldo:
        for nama, s in lw_saldo.items():
            saldo[nama] = s

    for nama, angka in pemain_list:
        angka_huruf = re.findall(r'([0-9]+)\s*([A-Za-z]*)', angka)[0]
        num = int(angka_huruf[0])
        huruf = angka_huruf[1].upper()

        if menang:
            if huruf in ['P', 'LF']:
                nilai = num - calculate_fee(num, fee_percent, is_double=False)
                saldo[nama] += int(nilai)
            else:
                nilai = (num * 2) - calculate_fee(num, fee_percent, is_double=True)
                saldo[nama] += int(nilai)
        else:
            if huruf in ['P', 'LF']:
                saldo[nama] -= num
            else:
                # kalau ada saldo di LW, tetap dikurangi
                if lw_saldo and nama in lw_saldo and lw_saldo[nama] > 0:
                    saldo[nama] -= num
                # kalau gaada saldo di LW → dilewati

    return saldo

# ==============================
#   DECORATOR KHUSUS HOST
# ==============================
def require_host_active(func):
    """
    Decorator: hanya host aktif (rekapwin awal) yang boleh pakai perintah
    sampai dilakukan /resetlw.
    - Host wajib admin/owner saat pertama kali ditentukan.
    - Non-admin → bot diam saja.
    - Admin lain (bukan host) → diberi tahu.
    """
    def wrapper(message, *args, **kwargs):
        chat_id = message.chat.id
        user_id = message.from_user.id

        host_id = current_game_host.get(chat_id)
        if host_id:
            # Kalau sudah ada host → hanya host / owner
            if user_id != host_id and user_id != OWNER_USER_ID:
                try:
                    member = bot.get_chat_member(chat_id, user_id)
                    if member.status in ["administrator", "creator"]:
                        bot.reply_to(
                            message,
                            "kamu bukan perintah awal,hanya perintah awal yang bisa memakainya."
                        )
                    # kalau bukan admin → bot diam saja
                except:
                    return  # gagal cek → diam
                return
        else:
            # Belum ada host → hanya admin/owner yang boleh akses
            try:
                member = bot.get_chat_member(chat_id, user_id)
                if not (member.status in ["administrator", "creator"] or user_id == OWNER_USER_ID):
                    return  # non-admin → bot diam saja
            except:
                return  # gagal cek → bot diam saja

        return func(message, *args, **kwargs)
    return wrapper

# ==========================================
# ---- handler utama /rekapwin ----
# ==========================================
@bot.message_handler(commands=command_case_insensitive(["rekapwin"]))
@require_host_or_admin
@require_host_active
def rekapwin_handler(msg):
    chat_id = msg.chat.id
    user_id = msg.from_user.id

    # Kalau mode reset aktif → reset semua data
    if reset_mode_active.get(chat_id):
        game_data[chat_id] = {
            "games": [], "dev": "", "rol": "",
            "last_win": "", "saldo": defaultdict(int), "history": []
        }
        pinned_message_id.pop(chat_id, None)
        rekapwin_owner.pop(chat_id, None)
        rekap_data.pop(chat_id, None)
        message_to_delete.pop(chat_id, None)
        current_game_host.pop(chat_id, None)
        reset_mode_active[chat_id] = False
        if chat_id in reset_mode_timer:
            reset_mode_timer[chat_id].cancel()
            reset_mode_timer.pop(chat_id, None)

    # ==== SINKRONISASI JIKA ADA PIN ====
    try:
        if not reset_mode_active.get(chat_id):   # hanya jalan kalau TIDAK reset
            pinned = bot.get_chat(chat_id).pinned_message
            if pinned and pinned.text:
                # ⛔ skip kalau pesan pin terakhir memang buatan bot sendiri
                if pinned.from_user.id != bot.get_me().id:
                    teks_pin = pinned.text.splitlines()
                    games, saldo = [], defaultdict(int)
                    dev, rol, last_win = "", "", ""

                    for line in teks_pin:
                        line_up = line.upper()
                        if line_up.startswith("DEV:"):
                            dev = line.replace("DEV:", "").strip()
                        elif line_up.startswith("ROL:"):
                            rol = line.replace("ROL:", "").strip()
                        elif line_up.startswith("LAST WIN"):
                            last_win = line.split(":")[-1].strip()
                        elif line_up.startswith("GAME "):
                            games.append(line.strip())
                        elif line and not line_up.startswith("SALDO"):
                            parts = line.split()
                            if len(parts) >= 2:
                                try:
                                    jumlah = float(parts[-1])
                                    nama = " ".join(parts[:-1]).upper()
                                    saldo[nama] = jumlah
                                except:
                                    pass

                    if games:  # kalau ada data game valid di pin
                        game_data[chat_id]["games"] = games
                        game_data[chat_id]["saldo"] = saldo
                        game_data[chat_id]["dev"] = dev
                        game_data[chat_id]["rol"] = rol
                        game_data[chat_id]["last_win"] = last_win
                        # ⚠️ Tidak usah kirim pesan sinkronisasi baru
    except Exception as e:
        print("Gagal sinkron pin:", e)

    # ==== ROLLBACK JIKA PESAN PIN SUDAH DIHAPUS ====
    try:
        if not reset_mode_active.get(chat_id):
            pinned = bot.get_chat(chat_id).pinned_message
            if not pinned:  # tidak ada pin sama sekali
                if game_data[chat_id]["history"]:
                    last_history = game_data[chat_id]["history"].pop()
                    game_data[chat_id]["saldo"] = defaultdict(int, last_history["prev_saldo"])
                    game_data[chat_id]["games"] = last_history["prev_games"]
    except Exception:
        if not reset_mode_active.get(chat_id):
            if game_data[chat_id]["history"]:
                last_history = game_data[chat_id]["history"].pop()
                game_data[chat_id]["saldo"] = defaultdict(int, last_history["prev_saldo"])
                game_data[chat_id]["games"] = last_history["prev_games"]

    # Kalau belum ada host, set host baru (tanpa kirim pesan)
    if chat_id not in current_game_host:
        current_game_host[chat_id] = user_id
    elif current_game_host[chat_id] != user_id:
        bot.reply_to(msg, "Hanya user rekapwin pertama yang bisa menggunakan.")
        return

    # Cek argumen fee (WAJIB ADA)
    parts = msg.text.split()
    if len(parts) < 2:
        bot.reply_to(msg, "Wajib sertakan fee. Contoh: /rekapwin 6")
        return
    try:
        fee_custom = float(parts[1])
    except:
        bot.reply_to(msg, "Fee harus angka (contoh: /rekapwin 6)")
        return

    if not msg.reply_to_message:
        bot.reply_to(msg, "Mohon balas pesan yang berisi data duel untuk direkap.")
        return

    isi = msg.reply_to_message.text.upper()

    # cari blok K dan B (urutan bebas)
    kecil_match = re.search(r'K[ECIL]*\s*:(.*?)(?=B[ESAR]*\s*:|$)', isi, re.DOTALL)
    besar_match = re.search(r'B[ESAR]*\s*:(.*?)(?=K[ECIL]*\s*:|$)', isi, re.DOTALL)

    if not kecil_match or not besar_match:
        bot.reply_to(msg, "Format salah. Harus ada K: dan B:.")
        return

    def parse_data(lines):
        hasil = []
        for line in lines:
            # strip dan bersihkan titik di akhir baris jika ada
            line_clean = line.strip().rstrip('.')
            part = line_clean.split()
            if len(part) >= 2:
                nama = part[0]
                angka = " ".join(part[1:])
                hasil.append((nama, angka))
        return hasil


    k_players = parse_data(kecil_match.group(1).strip().splitlines())
    b_players = parse_data(besar_match.group(1).strip().splitlines())

    # nomor game baru menyesuaikan kondisi terakhir
    game_id = len(game_data[chat_id]["games"]) + 1

    rekap_data[chat_id] = {
        "game_id": game_id,
        "k": k_players,
        "b": b_players,
        "fee": fee_custom,
        "user_id": msg.from_user.id,
        "cmd_msg_id": msg.message_id
    }
    rekapwin_owner[chat_id] = msg.from_user.id

    # kirim tombol win
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("KECIL", callback_data="win_K"),
        types.InlineKeyboardButton("BESAR", callback_data="win_B")
    )
    sent = bot.send_message(chat_id, "𝐏𝐢𝐥𝐢𝐡 𝐓𝐢𝐦 𝐏𝐞𝐦𝐞𝐧𝐚𝐧𝐠 :", reply_markup=markup, reply_to_message_id=msg.message_id)
    message_to_delete[chat_id]["main"] = sent.message_id

# ---- pilih win ----
@bot.callback_query_handler(func=lambda call: call.data.startswith("win_"))
def pilih_win(call):
    if not check_rekap_owner(call):
        return

    chat_id = call.message.chat.id
    rekap_data[chat_id]["win"] = call.data.split("_")[1]

    # tombol scor (atas) + tombol kembali (bawah)
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("2-0", callback_data="scor_2-0"),
        types.InlineKeyboardButton("2-1", callback_data="scor_2-1")
    )
    markup.row(
        types.InlineKeyboardButton("KEMBALI", callback_data="scor_back")
    )

    bot.edit_message_text(
        f"𝐏𝐞𝐦𝐞𝐧𝐚𝐧𝐠 {rekap_data[chat_id]['win']} 𝐝𝐞𝐧𝐠𝐚𝐧 𝐬𝐜𝐨𝐫:",
        chat_id,
        message_to_delete[chat_id]["main"],
        reply_markup=markup
    )


# ---- tombol kembali dari scor ke win ----
@bot.callback_query_handler(func=lambda call: call.data == "scor_back")
def scor_back(call):
    if not check_rekap_owner(call):
        return

    chat_id = call.message.chat.id
    # kembali ke pilihan win
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("𝙺𝙴𝙲𝙸𝙻", callback_data="win_K"),
        types.InlineKeyboardButton("𝙱𝙴𝚂𝙰𝚁", callback_data="win_B")
    )

    bot.edit_message_text(
        "𝐏𝐢𝐥𝐢𝐡 𝐓𝐢𝐦 𝐏𝐞𝐦𝐞𝐧𝐚𝐧𝐠 :",
        chat_id,
        message_to_delete[chat_id]["main"],
        reply_markup=markup
    )

    # hapus pilihan win biar bisa pilih ulang
    rekap_data[chat_id].pop("win", None)


# ---- pilih scor ----
@bot.callback_query_handler(func=lambda call: call.data.startswith("scor_"))
def pilih_scor(call):
    if not check_rekap_owner(call):
        return

    chat_id = call.message.chat.id
    rekap_data[chat_id]["scor"] = call.data.split("_")[1]

    # kalau belum ada rol (game pertama) → edit ke tombol rol
    if not game_data[chat_id]["rol"]:
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("𝚂𝙰𝙵𝙰𝚁𝙸", callback_data="rol_SAFARI"),
            types.InlineKeyboardButton("𝙶𝙾𝙾𝙶𝙻𝙴", callback_data="rol_GOOGLE"),
            types.InlineKeyboardButton("𝙲𝙷𝚁𝙾𝙼𝙴", callback_data="rol_CHROME")
        )
        bot.edit_message_text(
            "𝐏𝐢𝐥𝐢𝐡 𝐛𝐫𝐨𝐰𝐬𝐞𝐫 𝐲𝐚𝐧𝐠 𝐚𝐧𝐝𝐚 𝐠𝐮𝐧𝐚𝐤𝐚𝐧 𝐬𝐚𝐚𝐭 𝐢𝐧𝐢.",
            chat_id,
            message_to_delete[chat_id]["main"],
            reply_markup=markup
        )
    else:
        bot.delete_message(chat_id, message_to_delete[chat_id]["main"])
        kirim_rekap(chat_id, call.from_user)

# ---- pilih rol ----
@bot.callback_query_handler(func=lambda call: call.data.startswith("rol_"))
def pilih_rol(call):
    if not check_rekap_owner(call):
        return

    chat_id = call.message.chat.id
    game_data[chat_id]["rol"] = call.data.split("_")[1]

    # edit pesan tombol ke instruksi DEV (tidak kirim pesan baru lagi)
    bot.edit_message_text(
        "𝙎𝙞𝙡𝙖𝙠𝙖𝙣 𝙢𝙖𝙨𝙪𝙠𝙠𝙖𝙣 𝙣𝙖𝙢𝙖 𝙙𝙚𝙫𝙞𝙘𝙚 𝙮𝙖𝙣𝙜 𝙖𝙣𝙙𝙖 𝙜𝙪𝙣𝙖𝙠𝙖𝙣 (𝙗𝙖𝙡𝙖𝙨 𝙥𝙚𝙨𝙖𝙣 𝙞𝙣𝙞):",
        chat_id,
        message_to_delete[chat_id]["main"]
    )
    rekap_data[chat_id]["tunggu_dev"] = True

# ---- terima dev ----
@bot.message_handler(func=lambda msg: msg.chat.id in rekap_data and rekap_data[msg.chat.id].get("tunggu_dev"))
def simpan_dev(msg):
    chat_id = msg.chat.id

    # hanya boleh balas pesan bot yang terakhir (instruksi DEV)
    if not msg.reply_to_message or msg.reply_to_message.from_user.id != bot.get_me().id:
        return  # abaikan kalau bukan reply ke pesan bot

    # hanya user yang jalankan /rekapwin yang boleh lanjut
    if msg.from_user.id != rekap_data[chat_id].get("user_id"):
        bot.reply_to(msg, "𝙃𝙖𝙣𝙮𝙖 𝙪𝙨𝙚𝙧 𝙮𝙖𝙣𝙜 𝙢𝙚𝙣𝙟𝙖𝙡𝙖𝙣𝙠𝙖𝙣 𝙥𝙚𝙧𝙞𝙣𝙩𝙖𝙝 /rekapwin 𝙮𝙖𝙣𝙜 𝙗𝙞𝙨𝙖 𝙢𝙚𝙡𝙖𝙣𝙟𝙪𝙩𝙠𝙖𝙣.")
        return

    # simpan device
    game_data[chat_id]["dev"] = msg.text.strip().upper()
    rekap_data[chat_id].pop("tunggu_dev", None)

    kirim_rekap(chat_id, msg.from_user)

# ---- kirim rekap ----
def kirim_rekap(chat_id, user):
    data = rekap_data.pop(chat_id, None)
    if not data:
        bot.send_message(chat_id, "Data rekap tidak ditemukan.")
        rekapwin_owner.pop(chat_id, None)
        return

    game_id, win, scor, k, b = (
        data["game_id"], data["win"], data["scor"], data["k"], data["b"]
    )
    fee_percent = data.get("fee", 6)
    cmd_msg_id = data.get("cmd_msg_id")

    sisi_menang = k if win == "K" else b
    sisi_kalah = b if win == "K" else k
    total_menang = sum([clean_number(x[1]) for x in sisi_menang])

    # total fee tetap dihitung (untuk saldo internal), tapi tidak ditampilkan
    total_fee_game = 0
    for nama, angka in sisi_menang:
        angka_huruf = re.findall(r'([0-9]+)\s*([A-Za-z]*)', angka)[0]
        num = int(angka_huruf[0])
        huruf = angka_huruf[1].upper()
        if huruf in ["P", "LF"]:
            total_fee_game += calculate_fee(num, fee_percent, is_double=False)
        else:
            total_fee_game += calculate_fee(num, fee_percent, is_double=True)

    # simpan total fee kumulatif
    if "total_fee" not in game_data[chat_id]:
        game_data[chat_id]["total_fee"] = 0
    game_data[chat_id]["total_fee"] += total_fee_game

    # simpan total BL kumulatif
    if "total_bl" not in game_data[chat_id]:
        game_data[chat_id]["total_bl"] = 0
    game_data[chat_id]["total_bl"] += total_menang

    # simpan rincian BL per game
    if "rincian_bl" not in game_data[chat_id]:
        game_data[chat_id]["rincian_bl"] = []
    game_data[chat_id]["rincian_bl"].append(total_menang)

    # update last win + history game
    prev_saldo, prev_games = dict(game_data[chat_id]["saldo"]), list(game_data[chat_id]["games"])
    game_data[chat_id]["last_win"] = f"@{user.username}" if user.username else user.first_name

    # ⚠️ SUDAH DIUBAH: fee tidak ditampilkan di baris GAME
    game_data[chat_id]["games"].append(
        f"GAME {str(game_id).zfill(2)} : {win} {scor} {fmt_num(total_menang)}"
    )

    # update saldo pemain
    saldo = game_data[chat_id]["saldo"]
    for nama, jumlah in proses_saldo(
        sisi_menang, sisi_menang, sisi_kalah, menang=True, fee_percent=fee_percent
    ).items():
        if saldo[nama] < 0:
            hutang = abs(saldo[nama])
            saldo[nama] = jumlah - hutang if jumlah >= hutang else saldo[nama] + jumlah
        else:
            saldo[nama] += jumlah
    for nama, jumlah in proses_saldo(
        sisi_kalah, sisi_menang, sisi_kalah, menang=False, fee_percent=fee_percent
    ).items():
        if saldo[nama] > 0:
            if saldo[nama] >= abs(jumlah):
                saldo[nama] += jumlah
                if saldo[nama] == 0:
                    del saldo[nama]
            else:
                sisa = saldo[nama] + jumlah
                if sisa == 0:
                    del saldo[nama]
                else:
                    saldo[nama] = sisa
        else:
            saldo[nama] += jumlah

    # bersihkan saldo kosong
    saldo = {k: v for k, v in saldo.items() if v != 0}
    game_data[chat_id]["saldo"] = defaultdict(int, saldo)

    # format saldo
    positif = {k: v for k, v in saldo.items() if v > 0}
    negatif = {k: v for k, v in saldo.items() if v < 0}
    saldo_text = "".join(f"{n.upper()} {fmt_num(positif[n])}\n" for n in sorted(positif))
    if negatif:
        saldo_text += "\n𝙆𝘼𝙇𝘼𝙃 :\n" + "".join(f"{n.upper()} {fmt_num(negatif[n])}\n" for n in sorted(negatif))

    total = sum(positif.values())
    game_lines = "\n".join(game_data[chat_id]["games"])

    # teks notifikasi
    teks1 = f"✅ Rekap {win} dengan skor {scor} berhasil! Dilakukan oleh @{user.username or user.first_name}"

    # teks utama (dipin)
    teks2 = f"""𝘿𝙀𝙑 : {game_data[chat_id]["dev"]}
𝙍𝙊𝙇 : {game_data[chat_id]["rol"]}

𝙇𝘼𝙎𝙏 𝙒𝙄𝙉 : (@{user.username or user.first_name})
{game_lines}

SALDO PEMAIN : ({fmt_num(total)})\n𝙈𝙀𝙉𝘼𝙉𝙂 :
{saldo_text}
"""

    # kirim hasil
    bot.send_message(chat_id, teks1)
    sent = bot.send_message(chat_id, teks2, reply_to_message_id=cmd_msg_id)
    try:
        bot.pin_chat_message(chat_id, sent.message_id)
        pinned_message_id[chat_id] = sent.message_id
    except Exception:
        bot.send_message(
            chat_id,
            "⚠️ Bot tidak bisa menyematkan pesan.\n"
            "Berikan akses semat pesan ke bot agar rekap bisa otomatis dipin.",
            parse_mode="Markdown"
        )

    # simpan history
    game_data[chat_id]["history"].append({
        "game_id": game_id, "win": win, "scor": scor, "k": k, "b": b,
        "prev_saldo": prev_saldo, "prev_games": prev_games
    })
    message_to_delete.pop(chat_id, None)
    rekapwin_owner.pop(chat_id, None)

# ---- fitur tambahan: /depo (menampilkan saldo pemain jika tanpa jumlah, atau menambah saldo jika ada jumlah) ----
@bot.message_handler(commands=command_case_insensitive(["depo"]))
@require_host_or_admin
@require_host_active
def handle_depo(message):
    try:
        chat_id = message.chat.id
        parts = message.text.split(maxsplit=2)
        if len(parts) < 2:
            bot.reply_to(message, "Format salah.\nGunakan: `/depo Nama [Jumlah]` contoh: `/depo SANZ` atau `/depo SANZ 999`", parse_mode="Markdown")
            return

        nama_pemain = parts[1].strip()
        nama_upper = nama_pemain.upper()
        jumlah_tambah = None
        if len(parts) == 3:
            try:
                jumlah_tambah = float(parts[2])
                if jumlah_tambah <= 0:
                    bot.reply_to(message, "jumlah tambah harus positif.")
                    return
            except:
                bot.reply_to(message, "jumlah harus angka.")
                return

        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "Pesan bot tidak ada,gunakan /rekapwin untuk membuat lw.")
            return

        lines = pinned.text.split("\n")

        header_lines, saldo_lines = [], []
        saldo_section = False
        for line in lines:
            if line.strip().lower().startswith("saldo"):
                saldo_section = True
                continue
            if saldo_section:
                saldo_lines.append(line)
            else:
                header_lines.append(line)

        # Buat map saldo
        saldo_map = {}
        for line in saldo_lines:
            if line.strip():
                parts_line = line.split()
                if len(parts_line) >= 2:
                    nama_line = " ".join(parts_line[:-1]).upper()
                    try:
                        saldo_map[nama_line] = float(parts_line[-1])
                    except:
                        saldo_map[nama_line] = 0

        saldo_ada = nama_upper in saldo_map
        saldo_lama = saldo_map.get(nama_upper, 0)

        if jumlah_tambah is not None:
            saldo_baru = saldo_lama + jumlah_tambah
            if abs(saldo_baru) < 1e-9:
                saldo_map.pop(nama_upper, None)  # hapus kalau jadi 0
            else:
                saldo_map[nama_upper] = saldo_baru

        # Hitung ulang total positif
        total_positif = sum(v for v in saldo_map.values() if v > 0)

        # Urutkan saldo berdasarkan nama (abjad)
        saldo_new = [f"SALDO PEMAIN : ({fmt_num(total_positif)})"]
        for nama in sorted(saldo_map.keys()):
            val = saldo_map[nama]
            if val > 0:
                saldo_new.append(f"{nama} {fmt_num(val)}")
        if any(v < 0 for v in saldo_map.values()):
            saldo_new.append("")  # pisahkan kosong
            for nama in sorted(saldo_map.keys()):
                val = saldo_map[nama]
                if val < 0:
                    saldo_new.append(f"{nama} {fmt_num(val)}")

        # Gabungkan header + saldo baru
        new_text = "\n".join(header_lines + saldo_new)

        # Update pesan pin
        bot.edit_message_text(new_text, chat_id=chat_id, message_id=pinned.message_id)

        # Update saldo di memori bot
        if chat_id not in game_data:
            game_data[chat_id] = {
                "games": [], "dev": "", "rol": "", "last_win": "",
                "saldo": defaultdict(int), "history": []
            }
        game_data[chat_id]["saldo"] = saldo_map

        if jumlah_tambah is None:
            if saldo_ada:
                bot.reply_to(message, f"Saldo {nama_upper}: {fmt_num(saldo_lama)}")
            else:
                bot.reply_to(message, f"{nama_upper} belum ada saldo.")
        else:
            if saldo_ada:
                bot.reply_to(message, f"✅ Saldo {nama_upper} berhasil ditambah {fmt_num(jumlah_tambah)}")
            else:
                bot.reply_to(message, f"✅ Saldo baru untuk {nama_upper} ditambahkan {fmt_num(jumlah_tambah)}")

    except Exception as e:
        bot.reply_to(message, f"Gagal update saldo: {e}")
        
        
# ---- fitur tambahan: /lunas (menghapus pemain dari daftar saldo) ----
@bot.message_handler(commands=command_case_insensitive(["lunas"]))
@require_host_or_admin
@require_host_active
def handle_lunas(message):
    try:
        chat_id = message.chat.id
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Format salah.\nGunakan: `/lunas NamaPemain`", parse_mode="Markdown")
            return

        nama_pemain = parts[1].strip()
        nama_upper = nama_pemain.upper()

        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "Tidak ada pesan yang di-pin.")
            return

        lines = pinned.text.split("\n")
        updated_lines = []
        saldo_section = False

        for line in lines:
            if line.strip().lower().startswith("saldo"):
                saldo_section = True
                updated_lines.append(line)
                continue

            if saldo_section and line.strip():
                parts_line = line.split()
                if len(parts_line) >= 2:
                    # ambil semua kata kecuali angka terakhir
                    nama_line = " ".join(parts_line[:-1]).upper()
                    if nama_line != nama_upper:  # hanya hapus kalau persis sama
                        updated_lines.append(line)
                else:
                    updated_lines.append(line)
            else:
                updated_lines.append(line)

        new_text = "\n".join(updated_lines)
        bot.edit_message_text(new_text, chat_id=chat_id, message_id=pinned.message_id)

        # hapus saldo di memori bot
        if chat_id in game_data:
            game_data[chat_id]["saldo"].pop(nama_upper, None)

        bot.reply_to(message, f"✅ {nama_upper} sudah lunas dari saldo.")

    except Exception as e:
        bot.reply_to(message, f"Gagal menghapus saldo: {e}")
        
# ---- fitur tambahan: /gantidev (mengganti device di pesan pin) ----
@bot.message_handler(commands=command_case_insensitive(["gantidev"]))
@require_host_or_admin
@require_host_active
def handle_ganti_dev(message):
    try:
        chat_id = message.chat.id
        parts = message.text.split(maxsplit=1)

        if len(parts) < 2:
            bot.reply_to(message, 
                "Format salah.\nGunakan: `/gantidev NamaDeviceBaru`\nContoh: `/gantidev IPHONE 25 PRO`",
                parse_mode="Markdown"
            )
            return

        device_baru = parts[1].strip().upper()

        # cek pinned message
        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "Tidak ada pesan LW yang dipin. Gunakan /rekapwin dulu.")
            return

        lines = pinned.text.split("\n")
        updated = False

        # ganti baris DEV:
        for i, line in enumerate(lines):
            if line.upper().startswith("DEV:"):
                lines[i] = f"DEV: {device_baru}"
                updated = True
                break

        if not updated:
            bot.reply_to(message, "Tidak menemukan baris DEV: di pesan pin.")
            return

        # update memory bot
        if chat_id not in game_data:
            game_data[chat_id] = {
                "games": [], "dev": "", "rol": "", "last_win": "",
                "saldo": defaultdict(int), "history": []
            }

        game_data[chat_id]["dev"] = device_baru

        # edit pesan pin
        new_text = "\n".join(lines)
        bot.edit_message_text(new_text, chat_id=chat_id, message_id=pinned.message_id)

        bot.reply_to(message, f"✅ Device berhasil diganti menjadi:\n➡️ {device_baru}")

    except Exception as e:
        bot.reply_to(message, f"Gagal mengganti device: {e}")        

# ---- fitur tambahan: /wd (menampilkan saldo positif pemain) ----
@bot.message_handler(commands=['wd'])
@require_host_or_admin
@require_host_active
def handle_wd(message):
    try:
        chat_id = message.chat.id
        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "Tidak ada pesan yang di-pin.")
            return

        lines = pinned.text.split("\n")
        saldo_section = False
        saldo_data = {}
        total_positif = 0

        for line in lines:
            if line.strip().lower().startswith("saldo"):
                saldo_section = True
                continue

            if saldo_section and line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    nama = " ".join(parts[:-1]).upper()
                    try:
                        jumlah = float(parts[-1])
                        saldo_data[nama] = jumlah
                        if jumlah > 0:
                            total_positif += jumlah
                    except:
                        pass

        if saldo_data:
            total_str = fmt_num(total_positif)

            teks = f"𝙎𝘼𝙇𝘿𝙊 𝙋𝙀𝙈𝘼𝙄𝙉 : ({total_str})\n𝚆𝙳 𝙳𝚄𝙻𝚄 𝙱𝙾𝚂 :\n"
            positif = {k: v for k, v in saldo_data.items() if v > 0}
            negatif = {k: v for k, v in saldo_data.items() if v < 0}

            # saldo positif dulu
            for nama, jumlah in sorted(positif.items()):
                jumlah_str = fmt_num(jumlah)
                teks += f"{nama} {jumlah_str}\n"

            # saldo negatif di bawah
            if negatif:
                teks += "\n𝙺𝙰𝙻𝙰𝙷 𝙼𝚄𝙻𝚄 𝙽𝙹𝙸𝚁 :\n"
                for nama, jumlah in sorted(negatif.items()):
                    jumlah_str = fmt_num(jumlah)
                    teks += f"{nama} {jumlah_str}\n"
        else:
            teks = "Tidak ada data saldo."

        bot.reply_to(message, teks.strip())

    except Exception as e:
        bot.reply_to(message, f"Gagal menampilkan saldo: {e}")

# ---- fitur tambahan: /kurang (mengurangi saldo pemain) ----
@bot.message_handler(commands=command_case_insensitive(["kurang"]))
@require_host_or_admin
@require_host_active
def handle_kurang(message):
    try:
        chat_id = message.chat.id
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(
                message,
                "Format salah.\nGunakan: `/kurang Nama Jumlah`\nContoh: `/kurang sanz 10`",
                parse_mode="Markdown"
            )
            return

        nama_pemain = parts[1].strip()
        nama_upper = nama_pemain.upper()

        try:
            jumlah_kurang = float(parts[2])
            if jumlah_kurang <= 0:
                bot.reply_to(message, "Jumlah harus positif.")
                return
        except:
            bot.reply_to(message, "Jumlah harus angka.")
            return

        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "Tidak ada pesan yang di-pin.")
            return

        lines = pinned.text.split("\n")

        header_lines, saldo_lines = [], []
        saldo_section = False
        for line in lines:
            if line.strip().lower().startswith("saldo"):
                saldo_section = True
                continue
            if saldo_section:
                saldo_lines.append(line)
            else:
                header_lines.append(line)

        # Buat map saldo dari pinned
        saldo_map = {}
        for line in saldo_lines:
            if line.strip():
                parts_line = line.split()
                if len(parts_line) >= 2:
                    nama_line = " ".join(parts_line[:-1]).upper()
                    try:
                        saldo_map[nama_line] = float(parts_line[-1])
                    except:
                        saldo_map[nama_line] = 0

        # Kurangi saldo pemain (atau buat baru jika belum ada)
        saldo_lama = saldo_map.get(nama_upper, 0)
        saldo_baru = saldo_lama - jumlah_kurang

        if abs(saldo_baru) < 1e-9:
            saldo_map.pop(nama_upper, None)  # hapus kalau jadi 0
        else:
            saldo_map[nama_upper] = saldo_baru

        # Hitung ulang total positif
        total_positif = sum(v for v in saldo_map.values() if v > 0)

        # Susun ulang saldo urut abjad
        saldo_new = [f"SALDO PEMAIN : ({fmt_num(total_positif)})"]
        for nama in sorted(saldo_map.keys()):
            val = saldo_map[nama]
            if val > 0:
                saldo_new.append(f"{nama} {fmt_num(val)}")
        if any(v < 0 for v in saldo_map.values()):
            saldo_new.append("")  # pisahkan kosong
            for nama in sorted(saldo_map.keys()):
                val = saldo_map[nama]
                if val < 0:
                    saldo_new.append(f"{nama} {fmt_num(val)}")

        # Gabungkan header + saldo baru
        new_text = "\n".join(header_lines + saldo_new)

        # Update pesan pin
        bot.edit_message_text(new_text, chat_id=chat_id, message_id=pinned.message_id)

        # Update saldo di memori bot
        if chat_id not in game_data:
            game_data[chat_id] = {
                "games": [], "dev": "", "rol": "", "last_win": "",
                "saldo": defaultdict(int), "history": []
            }
        game_data[chat_id]["saldo"] = saldo_map

        bot.reply_to(message, f"✅ Saldo {nama_upper} berhasil dikurangi {fmt_num(jumlah_kurang)}")

    except Exception as e:
        bot.reply_to(message, f"Gagal update saldo: {e}")

# ---- fitur tambahan: /tutorrekapwin (tutorial lengkap penggunaan fitur REKAP WIN) ----
@bot.message_handler(commands=command_case_insensitive(["tutorrekapwin"]))
def handle_tutorrekapwin(message):
    # === Pesan 1 ===
    teks1 = (
        "📚 TUTORIAL REKAPWIN\n\n"
        "Cara Penggunaan:\n"
        "1. Balas pesan yang berisi data duel\n"
        "2. Ketik: /rekapwin atau /rekapwin [fee] atau /rekapwin nc\n\n"
        "Parameter yang Tersedia:\n"
        "• /rekapwin - Mode normal dengan fee default 5.5%\n"
        "• /rekapwin 6 - Mode normal dengan fee 6%\n"
        "• /rekapwin nc - Mode NO CUT (tanpa fee persentase)\n"
        "• /rekapwin 6 nc - Mode NO CUT dengan parameter tambahan (akan diabaikan)\n\n"
        "Perbedaan Mode:\n"
        "• NORMAL: Menggunakan fee persentase (default 5.5%)\n"
        "• NO CUT: Menggunakan fee tetap berdasarkan fungsi hitung_fee (jumlah // 10 + 1)\n\n"
        "Contoh Data Duel:\n"
        "```\n"
        "KECIL:\n"
        "John 100\n"
        "Jane 200 LF\n\n"
        "BESAR:\n"
        "Bob 150\n"
        "Alice 300\n"
        "```"
    )
    bot.reply_to(message, teks1, parse_mode="Markdown")

    # === Pesan 2 ===
    teks2 = (
        "📖 Tutorial Lengkap Penggunaan Fitur REKAP WIN\n\n"
        "1️⃣ Format Input Data Duel:\n"
        "```\n"
        "KECIL:\n"
        "TLER 100 LF\n"
        "FIRMA 50\n\n"
        "BESAR:\n"
        "KELAS 100\n"
        "BELAJAR 50 LF\n"
        "```\n\n"
        "2️⃣ Penjelasan Format:\n"
        "• Header tim harus menggunakan 'KECIL:' atau 'BESAR:' (atau 'K:' / 'B:')\n"
        "• Format nickname dan nominal:\n"
        "  - Nickname bisa lebih dari satu kata\n"
        "  - Ada 2 format penulisan:\n"
        "    1. [NICKNAME] [MODAL] // [HASIL]\n"
        "    2. [NICKNAME] [MODAL]\n"
        "  - Tambahkan 'LF' atau 'P' di belakang untuk pemain yg sudah ada saldo\n\n"
        "3️⃣ Cara Menggunakan /rekapwin:\n"
        "• Mode Normal: /rekapwin atau /rekapwin 5,5\n"
        "• Mode No Cut: /rekapwin nc\n"
        "• Contoh: /rekapwin 6 → fee 6%\n\n"
        "4️⃣ Mode Perhitungan:\n"
        "• Normal → Fee persentase\n"
        "• No Cut → Fee tetap (jumlah // 10 + 1)\n\n"
        "5️⃣ Format Output:\n"
        "```\n"
        "DEV: [NAMA_DEVICE]\n"
        "ROL: [BROWSER]\n"
        "MODE: NO CUT / NORMAL (Fee: X%)\n\n"
        "LAST WIN : (@username)\n"
        "GAME 1 : K/B [SKOR] [TOTAL_MODAL_TIM]\n\n"
        "SALDO PEMAIN : (total_saldo)\n"
        "NAMA1 jumlah1\n"
        "NAMA2 jumlah2\n\n"
        "NAMA3 - jumlah3\n"
        "```\n\n"
        "6️⃣ Fitur Tambahan:\n"
        "• /resetlw - Reset game\n"
        "• /lunas NAMA - Lunasi hutang\n"
        "• /tambah NAMA 100 - Tambah saldo\n"
        "• /kurang NAMA 50 - Kurangi saldo\n"
        "• /depo NAMA 200 - Deposit saldo\n"
        "• /wd NAMA - Cek saldo\n"
        "• /bulatkan - Bulatkan ke 100\n"
        "• /m1only [skor] - Edit skor game terakhir\n"
        "• /edit [kn/kr] [skor] - Edit tim K jadi KN/KR\n\n"
        "❗ Perhatian:\n"
        "• Bot harus admin grup utk bisa pin pesan\n"
        "• Semua perubahan tercatat dgn username pelaku\n"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Salin", switch_inline_query="Tutorial REKAP WIN"))

    bot.send_message(message.chat.id, teks2, parse_mode="Markdown", reply_markup=markup)

# ---- fitur tambahan: /resetlw (reset total + bisa dibatalkan max 10 menit) ----
@bot.message_handler(commands=command_case_insensitive(['resetlw']))
def reset_lw_handler(message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    # ==== hanya OWNER atau HOST rekapwin pertama yang bisa reset ====
    allowed_host = current_game_host.get(chat_id)
    if user_id != OWNER_USER_ID and user_id != allowed_host:
        if allowed_host:
            try:
                host_member = bot.get_chat_member(chat_id, allowed_host).user
                host_name = f"@{host_member.username}" if host_member.username else host_member.first_name
            except:
                host_name = "HOST_REKAPWIN_AWAL"
            bot.reply_to(message, f"Hanya {host_name} yang bisa menjalankan /resetlw.")
        else:
            bot.reply_to(message, "rekapwin sudah direset sebelumnya,dan setelahnya akan memulai dari game 1.")
        return
    # ===============================================================

    # ==== kalau reset sudah aktif → rollback (batalkan reset) ====
    if reset_mode_active.get(chat_id):
        backup = reset_mode_active.get(f"backup_{chat_id}")
        if backup:
            game_data[chat_id] = backup["game_data"]
            current_game_host[chat_id] = backup["host"]
            bot.send_message(
                chat_id,
                "Reset dibatalkan, semua data game & saldo sudah dikembalikan.",
                reply_to_message_id=message.message_id
            )
        else:
            bot.send_message(
                chat_id,
                "Waktu 10 menit habis, data lama sudah hilang permanen.",
                reply_to_message_id=message.message_id
            )
        reset_mode_active[chat_id] = False
        reset_mode_active.pop(f"backup_{chat_id}", None)
        if chat_id in reset_mode_timer:
            reset_mode_timer[chat_id].cancel()
            reset_mode_timer.pop(chat_id, None)
        return

    # ==== reset pertama → kosongkan data, simpan backup ====
    reset_mode_active[f"backup_{chat_id}"] = {
        "game_data": dict(game_data[chat_id]),
        "host": current_game_host.get(chat_id)
    }

    # kosongkan semua data biar mulai dari 0 lagi
    game_data[chat_id] = {
        "games": [], "dev": "", "rol": "",
        "last_win": "", "saldo": defaultdict(int), "history": []
    }
    if chat_id in rekap_data: del rekap_data[chat_id]
    if chat_id in pinned_message_id: del pinned_message_id[chat_id]
    if chat_id in message_to_delete: del message_to_delete[chat_id]
    if chat_id in rekapwin_owner: del rekapwin_owner[chat_id]
# hapus host supaya setelah reset bisa ganti lagi
    if chat_id in current_game_host:
        del current_game_host[chat_id]

    # aktifkan mode reset
    reset_mode_active[chat_id] = True

    import threading
    def auto_off():
        if reset_mode_active.get(chat_id):
            reset_mode_active[chat_id] = False
            reset_mode_active.pop(f"backup_{chat_id}", None)
            bot.send_message(
                chat_id,
                "⏰ Mode reset telah berakhir setelah 10 menit.",
                reply_to_message_id=message.message_id
            )
    t = threading.Timer(600, auto_off)
    t.start()
    reset_mode_timer[chat_id] = t

    bot.send_message(
        chat_id,
        "✅ Mode reset aktif!\n\n"
        "Perintah /rekapwin selanjutnya akan mengabaikan data pin dan memulai game baru.\n"
        "Mode reset akan berakhir dalam 10 menit.\n\n"
        "Untuk membatalkan mode reset, ketik /resetlw lagi.",
        reply_to_message_id=message.message_id
    )

# ---- fitur tambahan: /bulatkan (untuk membulatkan saldo kelipatan 100) ----
@bot.message_handler(commands=command_case_insensitive(["bulatkan"]))
@require_host_active
@require_host_or_admin
def handle_bulatkan(message):
    try:
        chat_id = message.chat.id
        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "Tidak ada pesan yang di-pin.")
            return

        lines = pinned.text.split("\n")
        updated_lines = []
        saldo_section = False
        total_positif = 0
        lost_from_bulatkan = 0

        from collections import defaultdict
        import math

        if chat_id in game_data:
            saldo_data = game_data[chat_id]["saldo"]
        else:
            saldo_data = defaultdict(int)

        for line in lines:
            if line.strip().lower().startswith("saldo"):
                saldo_section = True
                updated_lines.append(line)
                continue

            if saldo_section and line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    nama = " ".join(parts[:-1]).upper()
                    try:
                        jumlah = float(parts[-1])

                        # saldo positif → bulatkan turun ke kelipatan 100
                        if jumlah > 0:
                            if jumlah >= 100:
                                jumlah_baru = int(jumlah // 100 * 100)
                                lost_from_bulatkan += (jumlah - jumlah_baru)
                                if jumlah_baru != 0:
                                    total_positif += jumlah_baru
                                    updated_lines.append(f"{nama} {fmt_num(int(jumlah_baru))}")
                                    saldo_data[nama] = jumlah_baru
                                else:
                                    saldo_data.pop(nama, None)
                            else:
                                # +1..+99 → hapus
                                lost_from_bulatkan += jumlah
                                saldo_data.pop(nama, None)
                                continue

                        # saldo negatif → bulatkan menjauh dari 0 ke kelipatan 100
                        elif jumlah < 0:
                            jumlah_baru = int(math.floor(jumlah / 100.0) * 100)
                            if jumlah_baru != 0:
                                updated_lines.append(f"{nama} {fmt_num(int(jumlah_baru))}")
                                saldo_data[nama] = jumlah_baru
                            else:
                                saldo_data.pop(nama, None)
                                continue

                    except:
                        updated_lines.append(line)
                else:
                    updated_lines.append(line)
            else:
                updated_lines.append(line)

        # update header SALDO PEMAIN
        for i, line in enumerate(updated_lines):
            if line.strip().lower().startswith("saldo"):
                updated_lines[i] = f"SALDO PEMAIN : ({fmt_num(int(total_positif))})"
                break

        # update data memory
        game_data[chat_id]["saldo"] = saldo_data
        game_data[chat_id]["lost_bulatkan"] = lost_from_bulatkan

        # update pesan pin
        new_text = "\n".join(updated_lines)
        bot.edit_message_text(new_text, chat_id=chat_id, message_id=pinned.message_id)

        bot.reply_to(message, "✅ Saldo berhasil dibulatkan.")
    except Exception as e:
        bot.reply_to(message, f"Gagal membulatkan saldo: {e}")

# ---- fitur tambahan: /m1only ----
@bot.message_handler(commands=command_case_insensitive(["m1only"]))
@require_host_or_admin
@require_host_active
def handle_m1only(message):
    try:
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Format salah.\nGunakan: `/m1only skorBaru`\nContoh: `/m1only 23-43`", parse_mode="Markdown")
            return

        new_score = parts[1].strip()

        pinned = bot.get_chat(message.chat.id).pinned_message
        if not pinned:
            bot.reply_to(message, "pesan yang dipin bukan pesan bot.")
            return

        lines = pinned.text.split("\n")
        import re

        # Cari indeks game terakhir di lines
        last_game_index = None
        for i in reversed(range(len(lines))):
            if lines[i].strip().startswith("GAME "):
                last_game_index = i
                break

        if last_game_index is None:
            bot.reply_to(message, "Tidak ditemukan baris GAME untuk diubah.")
            return

        # Ubah skor di baris game terakhir
        line = lines[last_game_index]
        m = re.match(r"(GAME\s+\d+\s*:\s*[BK])\s+(\S+)(.*)", line)
        if not m:
            bot.reply_to(message, "Format baris GAME tidak sesuai.")
            return

        prefix = m.group(1)
        suffix = m.group(3)  # sisanya termasuk total balance

        lines[last_game_index] = f"{prefix} {new_score}{suffix}"

        # Edit pesan pin
        new_text = "\n".join(lines)
        bot.edit_message_text(new_text, chat_id=message.chat.id, message_id=pinned.message_id)

        # Update game_data di game terakhir juga
        chat_id = message.chat.id
        if chat_id in game_data:
            # Cari game terakhir yang sama prefix
            for idx in reversed(range(len(game_data[chat_id]["games"]))):
                g = game_data[chat_id]["games"][idx]
                mg = re.match(r"(GAME\s+\d+\s*:\s*[BK])\s+(\S+)(.*)", g)
                if mg and mg.group(1) == prefix:
                    game_data[chat_id]["games"][idx] = f"{prefix} {new_score}{mg.group(3)}"
                    break

        bot.reply_to(message, f"✅ Scor berhasil diubah {new_score}")

    except Exception as e:
        bot.reply_to(message, f"Gagal update skor game terakhir: {e}")

@bot.message_handler(commands=['start'])
def start(message):
    user = message.from_user
    chat_id = message.chat.id

    teks = (
        "🤖 *Selamat datang di Bot SANZ!*\n\n"
        "Bot ini dapat membantu anda untuk mengelola grup dengan fitur:\n"
        "•📊 Rekap kemenangan\n"
        "•📢 Broadcast pesan\n"
        "•🛡️ Anti-link protection\n"
        "•👥 Manajemen grup\n"
        "•🎯 Tag admin\n\n"
        "Untuk menggunakan bot ini di grup Anda,\n"
        "silakan klik tombol di bawah 👇"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton(
            "➕ Tambahkan ke Grup",
            url=f"https://t.me/{bot.get_me().username}?startgroup=true"
        )
    )

    bot.send_message(chat_id, teks, reply_markup=markup, parse_mode="Markdown")

    # Pesan 2 (promosi fitur BC)
    promo = (
        "📢 PROMO FITUR Rekap Otomatis/rekpwin\n\n"
        "Gunakan fitur /rekapwin di grup!\n\n"
        "Perintah yang tersedia:\n\n"
        "• Untuk fitur /rekapwin dan lain lain untuk harga mulai :\n"
        "- 20K / Bulan (minat hubungi developer/ketik /sewabot\n"
        "- 800K / Permanen (minat hubungi developer/ketik /sewabot"
    )
    bot.send_message(chat_id, parse_mode="Markdown")
    
@bot.message_handler(commands=['rules'])
def start(message):
    user = message.from_user
    chat_id = message.chat.id
    # Pesan 1 (welcome)
    bot.send_message(chat_id, f"Aturan Bot:"
    
     "1. Delay Tertentu: Bot ini memiliki delay acak antara 10 hingga 25 detik yang diset oleh developer."
     
    "2. Jangan Hapus Pesan: Jika bot membagikan sesuatu di grup, harap jangan menghapus pesan tersebut. Jika developer mengetahui adanya penghapusan pesan, grup Anda mungkin akan diblacklist oleh developer. Jika Anda memiliki pertanyaan lebih lanjut, silakan hubungi developer.. Thanks For Buyer")
    bot.send_message(chat_id, promo, parse_mode="Markdown")    

# =========================
# DATA SEWA BOT
# =========================

sewa_orders = {}
sewabot_map = {}
payment_map = {}

OWNER_ID = 7230983425

API_KEY = "FR3_ilgta8907052026tmjawhqqeioovo"
BASE_URL = "https://fr3newera.com/api/v1"

HARGA_SEWA = {
    "1bulan": 1000,
    "permanen": 350000
}
# =========================
# File Penyimpanan
# =========================
def save_sewabot():
    with open("sewabot.txt", "w") as f:
        for chat_id, data in sewabot_map.items():
            expire = data["expire"] if data["expire"] else "None"
            f.write(f"{chat_id}|{data['paket']}|{expire}\n")

def load_sewabot():
    try:
        with open("sewabot.txt") as f:
            for line in f:
                chat_id, paket, expire = line.strip().split("|")
                if expire == "None":
                    expire = None
                else:
                    expire = int(expire)
                sewabot_map[chat_id] = {"paket": paket, "expire": expire}
    except FileNotFoundError:
        pass

load_sewabot()

# =========================
# CREATE PAYMENT QRIS
# =========================
def create_payment(chat_id, amount):

    payload = {
        "apikey": API_KEY,
        "nominal": amount
    }

    try:

        url = f"{BASE_URL}/topup"

        r = requests.post(
            url,
            json=payload,
            timeout=30
        )

        print("STATUS CODE =", r.status_code)
        print("TEXT RESPONSE =", r.text)

        data = r.json()

        if data.get("status") != 200:
            return None

        paydata = data.get("data", {})

        trx_id = paydata.get("trxId")

        qr_string = paydata.get("qr_string")

        expired = paydata.get("expiry")

        return {
            "trx_id": trx_id,
            "qr": qr_string,
            "expired": expired
        }

    except Exception as e:
        print("CREATE PAYMENT ERROR =", e)
        return None
    
# =========================
# CANCEL PAYMENT
# =========================
def cancel_payment(trx_id):

    try:

        url = (
            f"{BASE_URL}/topup/cancel"
            f"?apikey={API_KEY}"
            f"&idTransaksi={trx_id}"
        )

        r = requests.get(url, timeout=20)

        print("CANCEL PAYMENT =", r.text)

        return True

    except Exception as e:
        print("CANCEL ERROR =", e)
        return False
# =========================
# AUTO CHECK PAYMENT
# =========================

def check_payment_loop():

    while True:

        try:

            for trx_id, data in list(payment_map.items()):

                url = (
                    f"{BASE_URL}/check-status"
                    f"?apikey={API_KEY}"
                    f"&idTransaksi={trx_id}"
                )

                r = requests.get(url, timeout=20)

                print("CHECK STATUS =", r.text)

                res = r.json()

                status = str(
                    res.get("data", {}).get("status", "")
                ).upper()

                print("PAYMENT STATUS =", status)

                if status in ["PAID", "SUCCESS", "BERHASIL"]:

                    chat_id = data["chat_id"]
                    paket = data["paket"]

                    if paket == "1bulan":
                        expire = int(time.time()) + (30 * 86400)
                    else:
                        expire = None

                    sewabot_map[chat_id] = {
                        "paket": paket,
                        "expire": expire
                    }

                    save_sewabot()

                    bot.send_message(
                        int(chat_id),
                        f"✅ PEMBAYARAN BERHASIL\n\n"
                        f"📦 Paket: {paket}\n"
                        f"🤖 Bot sudah aktif."
                    )

                    del payment_map[trx_id]

                elif status in ["EXPIRED", "CANCEL"]:

                    bot.send_message(
                        int(data["chat_id"]),
                        "❌ PAYMENT EXPIRED / DIBATALKAN"
                    )

                    del payment_map[trx_id]

        except Exception as e:
            print("CHECK PAYMENT ERROR =", e)

        time.sleep(15)  
    
def is_owner(user_id):
    return str(user_id) == str(OWNER_ID)

# =========================
# Cek Status Sewa
# =========================
def is_sewa_aktif(chat_id: str) -> bool:
    if chat_id not in sewabot_map:
        return False
    
    data = sewabot_map[chat_id]
    if data["paket"] == "permanen":
        return True
    
    if data["paket"] == "1bulan" and data["expire"]:
        return data["expire"] > int(time.time())
    
    return False

@bot.message_handler(commands=["sewabot"])
def handle_sewabot(message):

    if message.chat.type not in ["group", "supergroup"]:
        return

    markup = types.InlineKeyboardMarkup()

    btn1 = types.InlineKeyboardButton(
        "SEWA 1 BULAN",
        callback_data="pay_1bulan"
    )

    btn2 = types.InlineKeyboardButton(
        "SEWA PERMANEN",
        callback_data="pay_permanen"
    )

    markup.add(btn1)
    markup.add(btn2)

    bot.reply_to(
        message,
        "📦 PILIH PAKET SEWA BOT",
        reply_markup=markup
    )
# =========================
# Callback pilih paket
# =========================
@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def payment_handler(call):

    paket = call.data.split("_")[1]

    amount = HARGA_SEWA[paket]

    chat_id = str(call.message.chat.id)

    pay = create_payment(chat_id, amount)

    if not pay:
        bot.answer_callback_query(
            call.id,
            "❌ Gagal membuat payment",
            show_alert=True
        )
        return

    payment_map[pay["trx_id"]] = {
        "chat_id": chat_id,
        "paket": paket
    }

    caption = (
        f"✅ PEMBAYARAN DIBUAT\n\n"
        f"📦 Paket: {paket}\n"
        f"💰 Harga: Rp{amount:,}\n"
        f"🆔 ID: {pay['trx_id']}\n\n"
        f"⏳ Menunggu pembayaran..."
    ).replace(",", ".")

    qr_string = pay.get("qr")

    if qr_string:

        qr = qrcode.make(qr_string)

        path = f"qris_{pay['trx_id']}.png"

        qr.save(path)

        with open(path, "rb") as photo:

            markup = types.InlineKeyboardMarkup()

            batal = types.InlineKeyboardButton(
                "❌ BATALKAN",
                callback_data=f"cancelpay_{pay['trx_id']}"
            )

            markup.add(batal)

            bot.send_photo(
                call.message.chat.id,
                photo,
                caption=caption,
                reply_markup=markup
            )

        os.remove(path)

    else:

        bot.send_message(
            call.message.chat.id,
            caption
        )

    bot.answer_callback_query(call.id)  
    
@bot.callback_query_handler(func=lambda call: call.data.startswith("cancelpay_"))
def cancel_payment_handler(call):

    trx_id = call.data.split("_")[1]

    cancel_payment(trx_id)

    if trx_id in payment_map:
        del payment_map[trx_id]

    bot.edit_message_caption(
        caption="❌ PAYMENT DIBATALKAN",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )

    bot.answer_callback_query(
        call.id,
        "Payment dibatalkan"
    )
# ==============================
#   HANDLE BUKTI (SEWA BOT)
# ==============================
@bot.message_handler(content_types=["photo", "document"])
def handle_bukti_sewa(message):
    chat_id = str(message.chat.id)
    if message.chat.type not in ["group", "supergroup"]:
        return
    if chat_id not in sewa_orders:
        return

    qris_msg_id = sewa_orders[chat_id].get("qris_msg_id")
    if not message.reply_to_message or message.reply_to_message.message_id != qris_msg_id:
        bot.reply_to(message, "⚠️ Untuk kirim bukti pembayaran, wajib reply ke pesan QRIS.")
        return

    paket = sewa_orders[chat_id]["paket"]

    # forward bukti ke owner
    fwd = bot.forward_message(OWNER_ID, int(chat_id), message.message_id)
    payment_map[fwd.message_id] = {
        "chat_id": chat_id,
        "bukti_msg_id": message.message_id,
        "paket": paket,
        "expire_timer": None
    }

    # tombol KONFIRMASI & GAGAL
    markup = types.InlineKeyboardMarkup()
    btn_konfirmasi = types.InlineKeyboardButton("KONFIRMASI", callback_data=f"pay_ok_{fwd.message_id}")
    btn_gagal = types.InlineKeyboardButton("GAGAL", callback_data=f"pay_fail_{fwd.message_id}")
    markup.add(btn_konfirmasi, btn_gagal)

    bot.send_message(
        OWNER_ID,
        f"📥 Bukti pembayaran SEWA BOT dari grup: {message.chat.title}\n"
        f"🆔 Chat ID: {chat_id}\n"
        f"📦 Paket: {paket}\n\n"
        "Klik tombol di bawah untuk memproses:",
        reply_markup=markup
    )

    # balasan bot ke grup → reply ke foto/doc bukti
    info_msg = bot.send_message(
        message.chat.id,
        "✅ Bukti pembayaran sudah dikirim ke owner, tunggu konfirmasi.\nEstimasi (10-15 menit).",
        reply_to_message_id=message.message_id
    )
    sewa_orders[chat_id]["last_info_msg_id"] = info_msg.message_id

    # ========== TIMER 5 MENIT ========== #
    import threading
    def auto_expire():
        # jika masih ada di payment_map setelah 5 menit
        if fwd.message_id in payment_map:
            bot.send_message(
                int(chat_id),
                "⏰ Waktu 5 menit habis, bukti pembayaran tidak diproses.\n"
                "👉 Silakan ulangi proses dengan mengetik /sewabot lagi.",
                reply_to_message_id=message.message_id
            )
            # hapus data
            payment_map.pop(fwd.message_id, None)
            sewa_orders.pop(chat_id, None)

    t = threading.Timer(300, auto_expire)  # 300 detik = 5 menit
    t.start()
    payment_map[fwd.message_id]["expire_timer"] = t


# ==============================
#   OWNER KONFIRMASI / GAGAL
# ==============================
@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def handle_payment_action(call):
    if call.from_user.id != OWNER_ID:
        return bot.answer_callback_query(call.id, "Hanya owner yang bisa memproses.", show_alert=True)

    _, action, fwd_id = call.data.split("_")
    fwd_id = int(fwd_id)

    if fwd_id not in payment_map:
        return bot.answer_callback_query(call.id, "Data pembayaran tidak ditemukan.", show_alert=True)

    data = payment_map[fwd_id]
    chat_id, paket, bukti_msg_id = data["chat_id"], data["paket"], data["bukti_msg_id"]

    # hentikan timer auto-expire
    if data.get("expire_timer"):
        data["expire_timer"].cancel()

    try:
        chat = bot.get_chat(int(chat_id))
        nama_grup = chat.title
    except:
        nama_grup = "(nama grup tidak bisa diambil)"

    if action == "ok":
        # === KONFIRMASI ===
        bot.answer_callback_query(call.id, "✅ Konfirmasi berhasil.")  # langsung stop loading

        if paket == "1bulan":
            expire = int((datetime.now() + timedelta(days=30)).timestamp())
            expire_text = time.strftime("%d-%m-%Y %H:%M", time.localtime(expire))
        else:
            expire, expire_text = None, None

        sewabot_map[chat_id] = {"paket": paket, "expire": expire}
        save_sewabot()

        sewa_orders.pop(chat_id, None)
        payment_map.pop(fwd_id, None)

        # kirim pesan sukses → reply ke bukti
        if paket == "1bulan":
            bot.send_message(
                int(chat_id),
                f"✅ Bot sudah aktif 1 Bulan.\n⏳ Expire: {expire_text}\nTerima kasih sudah berlangganan di bot SANZ.",
                reply_to_message_id=bukti_msg_id
            )
        else:
            bot.send_message(
                int(chat_id),
                "✅ Bot sudah aktif permanen di grup ini.\nTerima kasih sudah berlangganan di bot SANZ.",
                reply_to_message_id=bukti_msg_id
            )

        # edit pesan owner
        bot.edit_message_text(
            f"✅ Pembayaran dikonfirmasi.\n\n"
            f"👥 Grup: {nama_grup}\n"
            f"🆔 ID: {chat_id}\n"
            f"📦 Paket: {paket}",
            call.message.chat.id,
            call.message.message_id
        )
# === AUTO KIRIM TESTI KE GROUP TESTI ===
        try:
            TESTI_GROUP_ID = -1003359924960  # isi id grup testi

            # ambil waktu saat konfirmasi
            waktu_sekarang = time.strftime("%d-%m-%Y • %H:%M:%S", time.localtime())

            if paket == "1bulan":
                testi_text = (
                    "📢 *TESTI SEWA BOT BERHASIL*\n\n"
                    f"👥 Grup: {nama_grup}\n"
                    f"🆔 ID: `{chat_id}`\n"
                    f"📦 Paket: 1 Bulan\n"
                    f"⏳ Aktif sampai: {expire_text}\n"
                    f"📅 Waktu Konfirmasi: {waktu_sekarang}\n\n"
                    "Terima kasih telah berlangganan! ❤️"
                )
            else:
                testi_text = (
                    "📢 *TESTI SEWA BOT BERHASIL*\n\n"
                    f"👥 Grup: {nama_grup}\n"
                    f"🆔 ID: `{chat_id}`\n"
                    "📦 Paket: Permanen\n"
                    f"📅 Waktu Konfirmasi: {waktu_sekarang}\n\n"
                    "Terima kasih telah berlangganan! ❤️"
                )

            bot.send_message(TESTI_GROUP_ID, testi_text, parse_mode="Markdown")

        except Exception as e:
            print("Gagal kirim testi:", e)        

    elif action == "fail":
        # === GAGAL ===
        bot.answer_callback_query(call.id, "Pembayaran ditolak.")  # langsung stop loading

        try:
            last_info = sewa_orders[chat_id].get("last_info_msg_id")
            if last_info:
                bot.delete_message(int(chat_id), last_info)
        except:
            pass

        # kirim 1 pesan gagal ke grup → reply ke bukti
        bot.send_message(
            int(chat_id),
            "Pembayaran SEWA BOT dinyatakan GAGAL.\n"
            "💳 Bukti tidak valid atau pembayaran tidak masuk.\n\n"
            "👉 Silakan ulangi proses dengan mengetik /sewabot lagi.",
            reply_to_message_id=bukti_msg_id
        )

        # bersihkan data
        sewa_orders.pop(chat_id, None)
        payment_map.pop(fwd_id, None)

        # edit pesan owner
        bot.edit_message_text(
            f"Pembayaran ditolak oleh owner.\n\n"
            f"👥 Grup: {nama_grup}\n"
            f"🆔 ID: {chat_id}\n"
            f"📦 Paket: {paket}",
            call.message.chat.id,
            call.message.message_id
        )

# =========================
# /ceksewabot
# =========================
@bot.message_handler(commands=["ceksewabot"])
def handle_ceksewa(message):
    chat_id = str(message.chat.id)
    if chat_id not in sewabot_map:
        bot.reply_to(message, "Grup ini belum berlangganan sewa bot.")
        return

    data = sewabot_map[chat_id]
    paket = data["paket"]
    expire = data["expire"]

    if paket == "1bulan" and expire:
        sisa_detik = expire - int(time.time())
        if sisa_detik <= 0:
            bot.reply_to(message, "Masa sewa sudah habis.")
        else:
            sisa_hari = sisa_detik // 86400
            if sisa_hari >= 1:
                bot.reply_to(
                    message,
                    f"📌 Status Sewa: 1 Bulan\n"
                    f"⏳ Sisa {sisa_hari} hari\n"
                    f"📅 Expire: {time.strftime('%d-%m-%Y %H:%M:%S', time.localtime(expire))}"
                )
            else:
                jam = (sisa_detik % 86400) // 3600
                menit = (sisa_detik % 3600) // 60
                detik = sisa_detik % 60
                bot.reply_to(
                    message,
                    f"📌 Status Sewa: 1 Bulan\n"
                    f"⏳ Sisa {jam:02d}:{menit:02d}:{detik:02d}\n"
                    f"📅 Expire: {time.strftime('%d-%m-%Y %H:%M:%S', time.localtime(expire))}"
                )
    else:
        bot.reply_to(message, "📌 Status Sewa: Permanen ✅")

# =========================
# Auto cek expired + reminder (7 hari & 3 hari)
# =========================
def cek_expired_loop():
    while True:
        now = int(time.time())

        for chat_id, data in list(sewabot_map.items()):
            if data["paket"] == "1bulan" and data["expire"]:
                sisa = data["expire"] - now
                hari = sisa // 86400

                # inisialisasi flag kalau belum ada
                if "reminder_sent" not in data:
                    data["reminder_sent"] = []

                # reminder 7 hari
                if hari == 7 and 7 not in data["reminder_sent"]:
                    try:
                        bot.send_message(
                            int(chat_id),
                            "⏳ Masa sewa bot akan habis *7 hari lagi*."
                        )
                        data["reminder_sent"].append(7)
                        save_sewabot()
                    except:
                        pass

                # reminder 3 hari
                elif hari == 3 and 3 not in data["reminder_sent"]:
                    try:
                        bot.send_message(
                            int(chat_id),
                            "⚠️ Masa sewa bot tinggal *3 hari lagi*."
                        )
                        data["reminder_sent"].append(3)
                        save_sewabot()
                    except:
                        pass

                # expired → hapus permanen
                if sisa <= 0:
                    try:
                        bot.send_message(
                            int(chat_id),
                            "Masa sewa bot sudah habis.\n"
                            "Silakan aktifkan kembali dengan /sewabot."
                        )
                    except:
                        pass

                    # hapus data expired
                    del sewabot_map[chat_id]
                    save_sewabot()

        time.sleep(3600)  # cek tiap 1 jam

# =========================
# /listsewabot
# =========================
@bot.message_handler(commands=["listsewabot"])
def handle_listsewabot(message):
    if message.from_user.id != OWNER_ID:
        return
    
    if not sewabot_map:
        bot.reply_to(message, "📭 Belum ada grup yang terdaftar sewa.")
        return

    teks = "📋 <b>Daftar Sewa Grup:</b>\n\n"
    total_perbulan = 0
    total_permanen = 0

    now = int(time.time())
    expired_keys = []  # simpan yg expired buat dihapus

    for gid, data in list(sewabot_map.items()):
        try:
            chat = bot.get_chat(int(gid))
            nama = chat.title
        except:
            nama = "(Nama grup tidak bisa diambil)"

        paket = data["paket"]
        if paket == "permanen":
            expire_str = "♾️ PERMANEN"
            total_permanen += 1
        else:
            sisa_detik = data["expire"] - now
            if sisa_detik <= 0:
                expired_keys.append(gid)  # tandai expired
                continue  # skip, jangan ditampilkan
            else:
                sisa_hari = sisa_detik // 86400
                if sisa_hari >= 1:
                    expire_str = f"⏳ {sisa_hari} hari lagi"
                else:
                    jam = (sisa_detik % 86400) // 3600
                    menit = (sisa_detik % 3600) // 60
                    detik = sisa_detik % 60
                    expire_str = f"⏳ {jam:02d}:{menit:02d}:{detik:02d} lagi"
            total_perbulan += 1

        teks += (
            f"👥 {nama}\n"
            f"🆔 <code>{gid}</code>\n"
            f"📦 Paket: {paket}\n"
            f"{expire_str}\n\n"
        )

    # hapus expired dari map dan simpan file
    for gid in expired_keys:
        del sewabot_map[gid]
    if expired_keys:
        save_sewabot()

    teks += (
        "━━━━━━━━━━━━━━\n"
        f"📊 Total Grup Perbulan: {total_perbulan}\n"
        f"♾️ Total Grup Permanen: {total_permanen}\n"
        f"📌 TOTAL: {total_perbulan + total_permanen} grup"
    )

    bot.reply_to(message, teks, parse_mode="HTML")

@bot.message_handler(commands=["delsewa"])
def handle_delsewa(message):
    # hanya owner yang boleh
    if message.from_user.id != OWNER_ID:
        return  # bot diam

    # hanya bisa di private chat
    if message.chat.type != "private":
        return  # bot diam

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.reply_to(message, "Format salah.\nGunakan: /delsewa <id_grup>")

    chat_id = parts[1].strip()

    # cek apakah ada di map
    if chat_id not in sewabot_map:
        return bot.reply_to(message, f"Grup {chat_id} tidak ditemukan di daftar sewa.")

    # coba ambil nama grup
    try:
        chat = bot.get_chat(int(chat_id))
        nama_grup = chat.title
    except:
        nama_grup = "(Nama grup tidak bisa diambil)"

    # hapus dari map
    del sewabot_map[chat_id]
    save_sewabot()

    teks = (
        f"👥 Grup : {nama_grup}\n"
        f"🆔 ID Grup : `{chat_id}`\n\n"
        f"✅ Grup berhasil dihapus dari daftar sewa."
    )
    bot.reply_to(message, teks, parse_mode="Markdown")

# =========================
# /tambahsewa (khusus OWNER via private chat)
# =========================
@bot.message_handler(commands=["tambahsewa"])
def handle_tambahsewa(message):
    if message.from_user.id != OWNER_ID:
        return  # orang lain -> bot diam
    if message.chat.type != "private":
        return  # hanya bisa di private chat

    markup = types.InlineKeyboardMarkup()
    btn1 = types.InlineKeyboardButton("1 Bulan", callback_data="tambahsewa_1bulan")
    btn2 = types.InlineKeyboardButton("Permanen", callback_data="tambahsewa_permanen")
    markup.row(btn1, btn2)

    msg = bot.reply_to(message, "📌 Pilih paket sewa yang ingin ditambahkan:", reply_markup=markup)
    sewa_orders[str(message.from_user.id)] = {"awaiting_paket": True, "msg_id": msg.message_id}


# callback pilih paket
@bot.callback_query_handler(func=lambda call: call.data.startswith("tambahsewa_"))
def handle_tambahsewa_paket(call):
    if call.from_user.id != OWNER_ID:
        return
    if call.message.chat.type != "private":
        return

    paket = call.data.split("_")[1]
    teks = f"✅ Paket *{paket}* dipilih.\n\n📌 Kirimkan ID grup yang perlu ditambahkan sewa (reply ke pesan ini)."

    bot.edit_message_text(teks, call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    sewa_orders[str(call.from_user.id)] = {"paket_manual": paket, "msg_id": call.message.message_id}


# handler input ID grup
@bot.message_handler(func=lambda m: str(m.from_user.id) in sewa_orders and m.reply_to_message and m.reply_to_message.message_id == sewa_orders[str(m.from_user.id)]["msg_id"])
def handle_tambahsewa_id(message):
    if message.from_user.id != OWNER_ID:
        return
    if message.chat.type != "private":
        return

    state = sewa_orders.get(str(message.from_user.id))
    if not state:
        return

    paket = state["paket_manual"]
    chat_id = message.text.strip()

    try:
        if paket == "1bulan":
            expire = int((datetime.now() + timedelta(days=30)).timestamp())
            expire_text = time.strftime("%d-%m-%Y %H:%M", time.localtime(expire))
        else:
            expire = None
            expire_text = "Permanen"

        sewabot_map[chat_id] = {"paket": paket, "expire": expire}
        save_sewabot()

        # Konfirmasi ke owner
        bot.reply_to(message, f"✅ Grup {chat_id} berhasil ditambahkan sewa ({paket}).\nExpire: {expire_text}")

        # Coba kirim pesan ke grup (jika bot sudah ada di dalamnya)
        try:
            if paket == "1bulan":
                bot.send_message(int(chat_id), f"✅ Bot sudah aktif 1 Bulan.\n⏳ Expire: {expire_text}\nTerima kasih sudah berlangganan di bot SANZ.")
            else:
                bot.send_message(int(chat_id), "✅ Bot sudah aktif permanen di grup ini.\nTerima kasih sudah berlangganan di bot SANZ.")
        except:
            bot.reply_to(message, f"Grup {chat_id} tidak bisa dikirimi pesan (mungkin bot belum join atau bukan admin).")

    except Exception as e:
        bot.reply_to(message, f"Gagal menambahkan sewa: {e}")

    del sewa_orders[str(message.from_user.id)]

# =========================
# /tambahmasasewa (khusus OWNER via private chat)
# =========================
@bot.message_handler(commands=["tambahmasasewa"])
def handle_tambahmasasewa_cmd(message):
    if message.from_user.id != OWNER_ID or message.chat.type != "private":
        return
    msg = bot.send_message(message.chat.id, "📌 Masukkan ID grup yang mau ditambah masa sewanya:")
    sewa_orders[str(message.from_user.id)] = {"mode": "tambah", "step": "id", "msg_id": msg.message_id}

# Step 2: owner kirim id grup
@bot.message_handler(func=lambda m: str(m.from_user.id) in sewa_orders and sewa_orders[str(m.from_user.id)]["step"] == "id" and sewa_orders[str(m.from_user.id)]["mode"] == "tambah")
def handle_tambahmasasewa_group(message):
    if message.from_user.id != OWNER_ID or message.chat.type != "private":
        return

    state = sewa_orders[str(message.from_user.id)]
    chat_id = message.text.strip()
    msg_id = state["msg_id"]

    if chat_id not in sewabot_map:
        bot.edit_message_text(f"⚠️ Grup {chat_id} tidak ditemukan di daftar sewa.",
                              chat_id=message.chat.id, message_id=msg_id)
        sewa_orders.pop(str(message.from_user.id), None)
        return

    paket = sewabot_map[chat_id]["paket"]
    if paket == "permanen":
        bot.edit_message_text("⚠️ Grup ini sewa permanen, tidak bisa ditambah masa sewanya.",
                              chat_id=message.chat.id, message_id=msg_id)
        sewa_orders.pop(str(message.from_user.id), None)
        return

    try:
        group_name = bot.get_chat(int(chat_id)).title
    except:
        group_name = "(Nama grup tidak bisa diambil)"

    teks = (
        f"👥 Nama Grup: {group_name}\n"
        f"🆔 ID Grup: {chat_id}\n\n"
        f"📌 Mau ditambah berapa hari? (contoh: 1, 2, 3)"
    )

    sewa_orders[str(message.from_user.id)] = {
        "mode": "tambah",
        "step": "days",
        "chat_id": chat_id,
        "msg_id": msg_id
    }
    bot.edit_message_text(teks, chat_id=message.chat.id, message_id=msg_id)

# Step 3: owner kirim jumlah hari
@bot.message_handler(func=lambda m: str(m.from_user.id) in sewa_orders and sewa_orders[str(m.from_user.id)]["step"] == "days" and sewa_orders[str(m.from_user.id)]["mode"] == "tambah")
def handle_tambahmasasewa_days(message):
    if message.from_user.id != OWNER_ID or message.chat.type != "private":
        return

    state = sewa_orders[str(message.from_user.id)]
    chat_id = state["chat_id"]
    msg_id = state["msg_id"]

    try:
        hari = int(message.text.strip())
        if hari <= 0:
            bot.edit_message_text("⚠️ Masukkan angka hari yang valid (>0).",
                                  chat_id=message.chat.id, message_id=msg_id)
            return
    except:
        bot.edit_message_text("⚠️ Harus angka, contoh: 2",
                              chat_id=message.chat.id, message_id=msg_id)
        return

    if chat_id in sewabot_map and sewabot_map[chat_id]["expire"]:
        sewabot_map[chat_id]["expire"] += hari * 86400
        save_sewabot()
        sisa_hari = (sewabot_map[chat_id]["expire"] - int(time.time())) // 86400
        teks = (
            f"✅ Masa sewa grup {chat_id} sudah ditambah {hari} hari.\n"
            f"⏳ Sisa: {sisa_hari} hari."
        )
        bot.edit_message_text(teks, chat_id=message.chat.id, message_id=msg_id)
    else:
        bot.edit_message_text(f"⚠️ Grup {chat_id} tidak memiliki data expire yang valid.",
                              chat_id=message.chat.id, message_id=msg_id)

    sewa_orders.pop(str(message.from_user.id), None)

# =========================
# /kurangmasasewa (khusus OWNER via private chat)
# =========================
@bot.message_handler(commands=["kurangmasasewa"])
def handle_kurangmasasewa_cmd(message):
    if message.from_user.id != OWNER_ID or message.chat.type != "private":
        return
    msg = bot.send_message(message.chat.id, "📌 Masukkan ID grup yang mau dikurangi masa sewanya:")
    sewa_orders[str(message.from_user.id)] = {"mode": "kurang", "step": "id", "msg_id": msg.message_id}

# Step 2: owner kirim id grup
@bot.message_handler(func=lambda m: str(m.from_user.id) in sewa_orders and sewa_orders[str(m.from_user.id)]["step"] == "id" and sewa_orders[str(m.from_user.id)]["mode"] == "kurang")
def handle_kurangmasasewa_group(message):
    if message.from_user.id != OWNER_ID or message.chat.type != "private":
        return

    state = sewa_orders[str(message.from_user.id)]
    chat_id = message.text.strip()
    msg_id = state["msg_id"]

    if chat_id not in sewabot_map:
        bot.edit_message_text(f"⚠️ Grup {chat_id} tidak ditemukan di daftar sewa.",
                              chat_id=message.chat.id, message_id=msg_id)
        sewa_orders.pop(str(message.from_user.id), None)
        return

    paket = sewabot_map[chat_id]["paket"]
    if paket == "permanen":
        bot.edit_message_text("♾️ Grup ini permanen, tidak bisa dikurangi masa sewanya.",
                              chat_id=message.chat.id, message_id=msg_id)
        sewa_orders.pop(str(message.from_user.id), None)
        return

    try:
        group_name = bot.get_chat(int(chat_id)).title
    except:
        group_name = "(Nama grup tidak bisa diambil)"

    teks = (
        f"👥 {group_name}\n"
        f"🆔 {chat_id}\n\n"
        f"📌 Mau dikurangi berapa hari? (contoh: 1, 2, 3)"
    )

    sewa_orders[str(message.from_user.id)] = {
        "mode": "kurang",
        "step": "days",
        "chat_id": chat_id,
        "msg_id": msg_id
    }
    bot.edit_message_text(teks, chat_id=message.chat.id, message_id=msg_id)

# Step 3: owner kirim jumlah hari
@bot.message_handler(func=lambda m: str(m.from_user.id) in sewa_orders and sewa_orders[str(m.from_user.id)]["step"] == "days" and sewa_orders[str(m.from_user.id)]["mode"] == "kurang")
def handle_kurangmasasewa_days(message):
    if message.from_user.id != OWNER_ID or message.chat.type != "private":
        return

    state = sewa_orders[str(message.from_user.id)]
    chat_id = state["chat_id"]
    msg_id = state["msg_id"]

    try:
        hari = int(message.text.strip())
        if hari <= 0:
            bot.edit_message_text("⚠️ Masukkan angka hari yang valid (>0).",
                                  chat_id=message.chat.id, message_id=msg_id)
            return
    except:
        bot.edit_message_text("⚠️ Harus angka, contoh: 2",
                              chat_id=message.chat.id, message_id=msg_id)
        return

    # kurangi masa sewa
    sewabot_map[chat_id]["expire"] -= hari * 86400
    if sewabot_map[chat_id]["expire"] < int(time.time()):
        sewabot_map[chat_id]["expire"] = int(time.time())

    save_sewabot()
    sisa_hari = (sewabot_map[chat_id]["expire"] - int(time.time())) // 86400
    teks = (
        f"✅ Masa sewa grup {chat_id} sudah dikurangi {hari} hari.\n"
        f"⏳ Sisa: {sisa_hari} hari."
    )
    bot.edit_message_text(teks, chat_id=message.chat.id, message_id=msg_id)

    sewa_orders.pop(str(message.from_user.id), None)

# =========================
# /upmasasewa (upgrade bulanan -> permanen)
# =========================
@bot.message_handler(commands=["upmasasewa"])
def handle_upmasasewa(message):
    if message.chat.type not in ["group", "supergroup"]:
        return bot.reply_to(message, "⚠️ Perintah ini hanya bisa dipakai di grup.")

    chat_id = str(message.chat.id)

    # cek admin
    try:
        member = bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in ["administrator", "creator"]:
            return  # anggota biasa diam saja
    except:
        return

    # hanya boleh upgrade dari bulanan
    if chat_id not in sewabot_map or sewabot_map[chat_id]["paket"] != "1bulan":
        return bot.reply_to(message, "⚠️ Hanya grup paket bulanan yang bisa upgrade ke permanen.")

    # simpan order
    sewa_orders[chat_id] = {"paket": "upgrade"}

    caption = (
        "📦 Upgrade Paket: Bulanan ➝ Permanen\n"
        "💰 Harga: Rp20.000\n\n"
        "💳 Silakan bayar dengan scan QRIS di bawah.\n"
        "👉 Setelah bayar, kirim bukti (foto/doc) *reply ke pesan ini*."
    )

    msg = bot.send_photo(message.chat.id, QRIS_URL, caption=caption, parse_mode="Markdown")
    sewa_orders[chat_id]["qris_msg_id"] = msg.message_id

    # === TIMER 5 MENIT (kalau tidak ada bukti) ===
    import threading
    def auto_expire():
        if chat_id in sewa_orders:
            bot.send_message(
                int(chat_id),
                "⏰ Waktu 5 menit habis, bukti pembayaran tidak dikirim.\n"
                "👉 Silakan ulangi proses dengan /upmasasewa.",
                reply_to_message_id=msg.message_id
            )
            sewa_orders.pop(chat_id, None)

    t = threading.Timer(300, auto_expire)
    t.start()
    sewa_orders[chat_id]["expire_timer"] = t


# =========================
# Handle bukti pembayaran upgrade
# =========================
@bot.message_handler(content_types=["photo", "document"])
def handle_bukti_upgrade(message):
    chat_id = str(message.chat.id)
    if message.chat.type not in ["group", "supergroup"]:
        return
    if chat_id not in sewa_orders or sewa_orders[chat_id]["paket"] != "upgrade":
        return

    qris_msg_id = sewa_orders[chat_id].get("qris_msg_id")
    if not message.reply_to_message or message.reply_to_message.message_id != qris_msg_id:
        return

    # stop timer karena bukti sudah ada
    if "expire_timer" in sewa_orders[chat_id]:
        sewa_orders[chat_id]["expire_timer"].cancel()
        del sewa_orders[chat_id]["expire_timer"]

    # forward bukti ke owner
    fwd = bot.forward_message(OWNER_ID, int(chat_id), message.message_id)
    payment_map[fwd.message_id] = {
        "chat_id": chat_id,
        "bukti_msg_id": message.message_id,
        "paket": "upgrade"
    }

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("KONFIRMASI", callback_data=f"up_ok_{fwd.message_id}")
    )

    bot.send_message(
        OWNER_ID,
        f"📥 Bukti UPGRADE PERMANEN dari grup: {message.chat.title}\n"
        f"🆔 Chat ID: {chat_id}\n\nKlik tombol di bawah untuk konfirmasi:",
        reply_markup=markup
    )
    bot.reply_to(message, "✅ Bukti pembayaran sudah dikirim ke owner, tunggu konfirmasi.")


# =========================
# Owner konfirmasi upgrade
# =========================
@bot.callback_query_handler(func=lambda call: call.data.startswith("up_ok_"))
def handle_upgrade_ok(call):
    if call.from_user.id != OWNER_ID:
        return bot.answer_callback_query(call.id, "Hanya owner yang bisa memproses.", show_alert=True)

    fwd_id = int(call.data.split("_")[2])
    if fwd_id not in payment_map:
        return bot.answer_callback_query(call.id, "Data upgrade tidak ditemukan.", show_alert=True)

    data = payment_map.pop(fwd_id)
    chat_id = data["chat_id"]
    bukti_msg_id = data["bukti_msg_id"]

    # set permanen
    sewabot_map[chat_id] = {"paket": "permanen", "expire": None}
    save_sewabot()
    sewa_orders.pop(chat_id, None)

    bot.send_message(
        int(chat_id),
        "✅ Upgrade berhasil! Grup ini sekarang *PERMANEN*.\nTerima kasih 🙏",
        reply_to_message_id=bukti_msg_id,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, "✅ Upgrade dikonfirmasi.")
    bot.edit_message_text(
        f"✅ Upgrade dikonfirmasi.\n👥 Grup ID: {chat_id}\n📦 Paket: PERMANEN",
        call.message.chat.id,
        call.message.message_id
    )

# ---- fitur tambahan: /tambah (menambah saldo pemain, saldo bisa positif saja) ----
@bot.message_handler(commands=command_case_insensitive(["tambah"]))
@require_host_or_admin
@require_host_active
def handle_tambah(message):
    try:
        chat_id = message.chat.id
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            bot.reply_to(message, "Format salah.\nGunakan: `/tambah Nama Jumlah` contoh: `/tambah SANZ 100`", parse_mode="Markdown")
            return

        nama_pemain = parts[1].strip()
        nama_upper = nama_pemain.upper()
        try:
            jumlah_tambah = float(parts[2])
            if jumlah_tambah <= 0:
                bot.reply_to(message, "Jumlah harus positif.")
                return
        except:
            bot.reply_to(message, "Jumlah harus angka.")
            return

        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "Tidak ada pesan yang di-pin.")
            return

        lines = pinned.text.split("\n")

        header_lines, saldo_lines = [], []
        saldo_section = False
        for line in lines:
            if line.strip().lower().startswith("saldo"):
                saldo_section = True
                continue
            if saldo_section:
                saldo_lines.append(line)
            else:
                header_lines.append(line)

        # Buat map saldo
        saldo_map = {}
        for line in saldo_lines:
            if line.strip():
                parts_line = line.split()
                if len(parts_line) >= 2:
                    nama_line = " ".join(parts_line[:-1]).upper()
                    try:
                        saldo_map[nama_line] = float(parts_line[-1])
                    except:
                        saldo_map[nama_line] = 0

        # Tambahkan saldo hanya ke nama penuh
        if nama_upper in saldo_map:
            saldo_map[nama_upper] += jumlah_tambah
        else:
            saldo_map[nama_upper] = jumlah_tambah

        # Hapus kalau saldo jadi nol
        if abs(saldo_map[nama_upper]) < 1e-9:
            saldo_map.pop(nama_upper)

        # Hitung total positif
        total_positif = sum(v for v in saldo_map.values() if v > 0)

        # Susun ulang saldo (positif di atas, negatif di bawah)
        saldo_new = [f"SALDO PEMAIN : ({fmt_num(total_positif)})"]
        for nama, val in sorted(saldo_map.items()):
            if val > 0:
                saldo_new.append(f"{nama} {fmt_num(val)}")
        if any(v < 0 for v in saldo_map.values()):
            saldo_new.append("")  # pisahkan kosong
            for nama, val in sorted(saldo_map.items()):
                if val < 0:
                    saldo_new.append(f"{nama} {fmt_num(val)}")

        # Gabungkan header + saldo baru
        new_text = "\n".join(header_lines + saldo_new)

        # Update pesan pin
        bot.edit_message_text(new_text, chat_id=chat_id, message_id=pinned.message_id)

        # Update saldo di memori bot
        if chat_id not in game_data:
            game_data[chat_id] = {
                "games": [], "dev": "", "rol": "", "last_win": "",
                "saldo": defaultdict(int), "history": []
            }
        game_data[chat_id]["saldo"][nama_upper] = saldo_map.get(nama_upper, 0)

        bot.reply_to(message, f"✅ Saldo {nama_upper} sudah ditambah {fmt_num(jumlah_tambah)}")

    except Exception as e:
        bot.reply_to(message, f"Gagal update saldo: {e}")

# ==============================
#   FITUR /contoh
# ==============================
@bot.message_handler(commands=["contoh"])
def handle_contoh(message):
    contoh_text = (
        "*Contoh Format:*\n\n"
        "*BESAR:*\n"
        "TEST 10\n"
        "TEST 20\n"
        "TEST 30\n"
        "TEST 40\n"
        "TEST 50\n\n"
        "*KECIL:*\n"
        "TEST 5\n"
        "TEST 15\n"
        "TEST 25\n"
        "TEST 35\n\n"
        "Penjelasan:\n"
        "- List harus memiliki lebih dari dua item.\n"
        "- Format harus memiliki titik dua (:) setelah setiap header seperti contoh 'BESAR:' dan 'KECIL:'.\n"
        "- Nick tidak boleh memiliki angka contoh 'TEST123 10'.\n"
        "- Harus memiliki spasi pada nick contoh 'TEST 10'.\n"
        "- Kalo ada saldo di lw kasi p.\n"
        "- Jika ingin menggunakan emoji harus ada spasi pada nick contoh 'TEST 10 😂'.\n\n"
        "👉 Ini buat lu yg dongo pake nya dan nyalahin bot nya padahal yg lain bisa."
    )

    bot.reply_to(message, contoh_text, parse_mode="Markdown")

# =========================
# /totalgrup (khusus OWNER via private chat)
# =========================
@bot.message_handler(commands=["totalgrup"])
def handle_totalgrup(message):
    if message.from_user.id != OWNER_ID:
        return  # hanya owner
    if message.chat.type != "private":
        return  # hanya di chat pribadi

    if not sewabot_map:
        bot.reply_to(message, "📭 Bot belum masuk grup manapun.")
        return

    teks = "📋 <b>Total Grup yang Bot Masuki:</b>\n\n"
    total = 0

    for gid in list(sewabot_map.keys()):
        try:
            chat = bot.get_chat(int(gid))
            nama = chat.title
            try:
                # ambil link invite kalau ada
                link = bot.export_chat_invite_link(int(gid))
            except:
                link = "(link tidak bisa diambil)"
        except:
            nama = "(Nama grup tidak bisa diambil)"
            link = "-"

        teks += (
            f"👥 {nama}\n"
            f"🆔 <code>{gid}</code>\n"
            f"🔗 {link}\n\n"
        )
        total += 1

    teks += f"━━━━━━━━━━━━━━\n📌 TOTAL: {total} grup"
    bot.reply_to(message, teks, parse_mode="HTML")

# ---- fitur tambahan: /rekap (untuk merekap data) ----
@bot.message_handler(commands=command_case_insensitive(["rekap"]))
def rekap_handler(msg):
    if not msg.reply_to_message:
        bot.reply_to(msg, "List nya mana bang, wajib reply.")
        return

    isi = msg.reply_to_message.text.upper()
    isi = isi.replace(".", "")
    blok = re.findall(r'([^\n:]+)\s*:(.*?)(?=[^\n:]+\s*:|$)', isi, re.DOTALL)

    if len(blok) < 2:
        bot.reply_to(msg, "Format salah. Harus ada minimal 2 blok data (contoh: K:, B:).")
        return

    team1_name, team1_data = blok[0][0].strip(), blok[0][1]
    team2_name, team2_data = blok[1][0].strip(), blok[1][1]

    # parsing nama + angka (support: "NAMA15P", "NAMA 15P", "NAMA 15 P", "NAMA 15 LF")
    def parse_data(lines):
        hasil = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            match = re.search(r'(\d+)\s*([A-Z]*)$', line)
            if match:
                angka = match.group(1) + match.group(2)  # simpan angka+huruf
                nama = line[:match.start()].strip()
                hasil.append((nama, angka))
        return hasil

    def ambil_angka(angka):
        try:
            return int(re.findall(r'\d+', angka)[0])  # cuma ambil angka depannya
        except:
            return 0

    team1_players = parse_data(team1_data.strip().splitlines())
    team2_players = parse_data(team2_data.strip().splitlines())

    team1_total = sum([ambil_angka(x[1]) for x in team1_players])
    team2_total = sum([ambil_angka(x[1]) for x in team2_players])
    saldo = team1_total + team2_total

    # bikin list angka (tanpa huruf) biar lebih rapi
    team1_list = ", ".join([str(ambil_angka(x[1])) for x in team1_players])
    team2_list = ", ".join([str(ambil_angka(x[1])) for x in team2_players])

    # Pesan 1 (rekapan dasar)
    teks1 = (
        f"🟢 {team1_name}: [{team1_list}] = {team1_total}\n\n"
        f"🔵 {team2_name}: [{team2_list}] = {team2_total}\n\n"
    )

    if team1_total == team2_total:
        teks1 += f"🐠 {team1_name} dan {team2_name} sudah seimbang!\n\n"
        teks1 += f"💰 Saldo Anda: {saldo} K"

        teks2 = (
            "𝘾𝙀𝙆 𝙉𝘼𝙈𝘼 + 𝙉𝙊𝙈𝙄𝙉𝘼𝙇 𝙈𝘼𝙎𝙄𝙉𝙂² 𝙆𝘼𝙇𝘼𝙐 𝙐𝘿𝘼𝙃 𝙈𝘼𝙀𝙉 𝘽𝘼𝙍𝙐 𝙋𝙍𝙊𝙏𝙀𝙎 𝘽𝙍𝙍𝙏𝙄 𝙃𝘼𝙉𝙂𝙐𝙎\n(𝙅𝙀𝙈𝙋𝙊𝙇 = 𝘼𝙈𝘼𝙉)"
        )

        bot.reply_to(msg, teks1)
        bot.send_message(msg.chat.id, teks2)

    elif team1_total > team2_total:
        selisih = team1_total - team2_total
        teks1 += f"🐠 {team2_name} masih -{selisih} untuk menyamakan {team1_name}\n\n"
        teks1 += f"💰 Saldo Anda: {saldo} K"

        teks2 = f"<b>{team2_name} -{selisih} ALL // ECER</b>"

        bot.reply_to(msg, teks1)
        bot.send_message(msg.chat.id, teks2, parse_mode="HTML")

    else:
        selisih = team2_total - team1_total
        teks1 += f"🐠 {team1_name} masih -{selisih} untuk menyamakan {team2_name}\n\n"
        teks1 += f"💰 Saldo Anda: {saldo} K"

        teks2 = f"<b>{team1_name} -{selisih} ALL // ECER</b>"

        bot.reply_to(msg, teks1)
        bot.send_message(msg.chat.id, teks2, parse_mode="HTML")

# ---- fitur tambahan: /geser (memindahkan saldo antar pemain, saldo bisa negatif) ----
@bot.message_handler(commands=command_case_insensitive(["geser"]))
@require_host_active
@require_host_or_admin
def handle_geser(message):
    try:
        chat_id = message.chat.id
        parts = message.text.split(maxsplit=3)
        if len(parts) < 4:
            bot.reply_to(
                message,
                "FORMAT SALAH.\nGunakan: `/geser NAMA ASAL JUMLAH NAMATUJUAN`\nContoh: `/geser SANZ 230 SZ`",
                parse_mode="Markdown"
            )
            return

        nama_asal = parts[1].strip().upper()
        try:
            jumlah = float(parts[2])
            if jumlah <= 0:
                bot.reply_to(message, "JUMLAH HARUS LEBIH DARI 0.")
                return
        except:
            bot.reply_to(message, "JUMLAH HARUS ANGKA.")
            return
        nama_tujuan = parts[3].strip().upper()

        pinned = bot.get_chat(chat_id).pinned_message
        if not pinned:
            bot.reply_to(message, "TIDAK ADA PESAN YANG DI-PIN. Jalankan /rekapwin dulu sampai ada saldo yang dipin.")
            return

        lines = pinned.text.split("\n")

        # Pisahkan header dan saldo
        header_lines, saldo_lines = [], []
        saldo_section = False
        for line in lines:
            if line.strip().lower().startswith("saldo"):
                saldo_section = True
                continue
            if saldo_section:
                saldo_lines.append(line)
            else:
                header_lines.append(line)

        # Buat map saldo
        saldo_map = {}
        for line in saldo_lines:
            if line.strip():
                parts_line = line.split()
                if len(parts_line) >= 2:
                    nama = " ".join(parts_line[:-1]).upper()
                    try:
                        saldo_map[nama] = float(parts_line[-1])
                    except:
                        saldo_map[nama] = 0

        # Pastikan asal dan tujuan ada
        if nama_asal not in saldo_map:
            saldo_map[nama_asal] = 0
        if nama_tujuan not in saldo_map:
            saldo_map[nama_tujuan] = 0

        # Proses geser saldo
        saldo_map[nama_asal] -= jumlah
        saldo_map[nama_tujuan] += jumlah

        # Hapus jika saldo 0
        saldo_map = {k: v for k, v in saldo_map.items() if abs(v) > 1e-9}

        # Hitung total positif
        total_positif = sum(v for v in saldo_map.values() if v > 0)

        # Susun ulang saldo (positif di atas, negatif di bawah)
        saldo_new = [f"SALDO PEMAIN : ({fmt_num(total_positif)})"]
        for nama, val in sorted(saldo_map.items()):
            if val > 0:
                saldo_new.append(f"{nama} {fmt_num(val)}")
        if any(v < 0 for v in saldo_map.values()):
            saldo_new.append("")
            for nama, val in sorted(saldo_map.items()):
                if val < 0:
                    saldo_new.append(f"{nama} {fmt_num(val)}")

        # Gabungkan header + saldo baru
        new_text = "\n".join(header_lines + saldo_new)

        # Update pesan pin
        bot.edit_message_text(new_text, chat_id=chat_id, message_id=pinned.message_id)

        # Update ke memori
        if chat_id not in game_data:
            game_data[chat_id] = {
                "games": [], "dev": "", "rol": "", "last_win": "",
                "saldo": defaultdict(int), "history": []
            }
        game_data[chat_id]["saldo"] = defaultdict(int, saldo_map)

        bot.reply_to(
            message,
            f"**SALDO {nama_asal} DIGESER KE {nama_tujuan} {fmt_num(jumlah)}**",
            parse_mode="Markdown"
        )

    except Exception as e:
        bot.reply_to(message, f"⚠️ ERROR DI FITUR /geser: {e}")

# ---- fitur tambahan: /totalfee (untuk menotal pendapatan) ----
@bot.message_handler(commands=command_case_insensitive(["totalfee"]))
@require_host_or_admin
@require_host_active
def handle_totalfee(message):
    chat_id = message.chat.id

    if chat_id not in game_data or "total_fee" not in game_data[chat_id]:
        bot.reply_to(message, "⚠️ Belum ada data fee. Gunakan /rekapwin dulu.")
        return

    total_fee_all = fmt_num(game_data[chat_id].get("total_fee", 0))
    total_bl_all = fmt_num(game_data[chat_id].get("total_bl", 0))
    total_game = len(game_data[chat_id].get("games", []))
    rincian_bl = game_data[chat_id].get("rincian_bl", [])

    tanggal = datetime.now().strftime("%d %B %Y")
    user = message.from_user

    teks = (
        f"📅 {tanggal}\n"
        f"👤 Users: @{user.username or user.first_name}\n"
        f"📊 Rincian BL: {rincian_bl}\n"
        f"🎮 Total Game: {total_game} Game\n"
        f"💰 Total BL: {total_bl_all}\n"
        f"💵 Total Keuntungan Fee (Belum termasuk pajak aplikasi): {total_fee_all}"
    )

    bot.reply_to(message, teks)

# ---- fitur tambahan: /win (rekap cepat dengan persen fee) ----
@bot.message_handler(commands=command_case_insensitive(["win"]))
def handle_win(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ Harus sertakan angka persen. Contoh: /win 5")
            return
        try:
            fee_percent = float(parts[1])
        except:
            bot.reply_to(message, "Fee harus berupa angka. Contoh: /win 5")
            return

        if not message.reply_to_message:
            bot.reply_to(message, "⚠️ Harus reply ke data duel K/B.")
            return

        teks = message.reply_to_message.text.upper()

        # cari blok K dan B (urutan bebas)
        pattern = re.compile(r'([KB](?:ECIL|ESAR)?\s*:)', re.IGNORECASE)
        parts_split = pattern.split(teks)
        blocks = []
        for i in range(1, len(parts_split), 2):
            label = parts_split[i].strip().replace(":", "")
            data = parts_split[i+1]
            blocks.append((label[0], data))  # ambil huruf pertama saja: K atau B

        if not blocks:
            bot.reply_to(message, "Format salah. Harus ada K: atau B:.")
            return

        def parse_block(block):
            hasil = []
            for line in block.strip().splitlines():
                if not line.strip():
                    continue
                part = line.strip().split()
                nama = part[0]
                angka = " ".join(part[1:])
                hasil.append((nama, angka))
            return hasil

        def hitung_player(players):
            out_lines = []
            total_fee = 0
            for nama, angka in players:
                angka_huruf = re.findall(r'([0-9]+)\s*([A-Za-z]*)', angka)[0]
                num = int(angka_huruf[0])
                huruf = angka_huruf[1].upper()

                if huruf in ['P','LF']:
                    fee = calculate_fee(num, fee_percent, is_double=False)
                    nilai = num - fee
                    out_lines.append(f"{nama} {num} {huruf} // {fmt_num(nilai)} {huruf}")
                else:
                    fee = calculate_fee(num, fee_percent, is_double=True)
                    nilai = (num * 2) - fee
                    out_lines.append(f"{nama} {num} // {fmt_num(nilai)}")

                total_fee += fee
            return "\n".join(out_lines), total_fee

        hasil = ""
        for label, data in blocks:
            players = parse_block(data)
            teks_hasil, fee_total = hitung_player(players)
            if label == "K":
                hasil += f"K:\n{teks_hasil}\nTotal fee yang didapat : {fmt_num(fee_total)}\n\n"
            else:
                hasil += f"B:\n{teks_hasil}\nTotal fee yang didapat : {fmt_num(fee_total)}\n\n"

        bot.reply_to(message, hasil.strip())

    except Exception as e:
        bot.reply_to(message, f"Error di /win: {e}")

@bot.message_handler(commands=["cekidgrup"])
def cekidgrup(message):
    if message.chat.type not in ["group", "supergroup"]:
        return bot.reply_to(message, "⚠️ Perintah ini hanya bisa dipakai di dalam grup.")

    user_id = message.from_user.id

    # cek admin / owner
    if user_id == OWNER_ID:
        is_admin = True
    else:
        try:
            member = bot.get_chat_member(message.chat.id, user_id)
            is_admin = member.status in ["administrator", "creator"]
        except:
            is_admin = False

    if not is_admin:
        return bot.reply_to(message, "⚠️ Hanya admin grup atau owner bot yang bisa pakai perintah ini.")

    grup_id = str(message.chat.id)

    # kirim pesan dengan format kode (abu-abu)
    bot.send_message(
        message.chat.id,
        f"🆔 Grup : `{grup_id}`",
        parse_mode="Markdown",
        reply_to_message_id=message.message_id
    )

# ==============================
#   HANDLER TOMBOL
# ==============================
@bot.callback_query_handler(func=lambda call: call.data.startswith("showid_"))
def callback_showid(call):
    grup_id = call.data.replace("showid_", "")
    # popup besar, hanya ID tanpa teks tambahan
    bot.answer_callback_query(
        call.id,
        grup_id,
        show_alert=True
    )

# ========== MENU PROTEKSI ==========
# Owner ID dan settings global
OWNER_ID = 7230983425
anti_settings = {}   # tempat simpan setting per grup

# ========== UTIL ==========
def ensure_chat_settings(chat_id):
    if chat_id not in anti_settings:
        anti_settings[chat_id] = {
            "antilink": False,
            "antiforward": False,
            "antitext": False,
            "blacklist": []
        }

def is_admin(chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except:
        return False

# alias biar pemanggilan lama tidak error
is_user_admin = is_admin


# ========== ANTILINK ==========
@bot.message_handler(commands=command_case_insensitive(["antilink"]))
@require_host_or_admin
def cmd_antilink(msg):
    chat_id = msg.chat.id
    ensure_chat_settings(chat_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("ON" if anti_settings[chat_id]["antilink"] else "ON", callback_data="antilink_on"),
        types.InlineKeyboardButton("OFF" if not anti_settings[chat_id]["antilink"] else "OFF", callback_data="antilink_off")
    )
    bot.reply_to(msg, "Atur *Antilink*:", reply_markup=kb, parse_mode="Markdown")

# ========== ANTIFORWARD ==========
@bot.message_handler(commands=["antiforward","antipesanteruskan"])
@require_host_or_admin
def cmd_antiforward(msg):
    chat_id = msg.chat.id
    ensure_chat_settings(chat_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("ON" if anti_settings[chat_id]["antiforward"] else "ON", callback_data="antiforward_on"),
        types.InlineKeyboardButton("OFF" if not anti_settings[chat_id]["antiforward"] else "OFF", callback_data="antiforward_off")
    )
    bot.reply_to(msg, "Atur *Anti Pesan Teruskan*:", reply_markup=kb, parse_mode="Markdown")

# ========== ANTITEXT TOGGLE ==========
@bot.message_handler(commands=command_case_insensitive(["antitext"]))
@require_host_or_admin
def cmd_antitext(msg):
    chat_id = msg.chat.id
    ensure_chat_settings(chat_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("ON" if anti_settings[chat_id]["antitext"] else "ON", callback_data="antitext_on"),
        types.InlineKeyboardButton("OFF" if not anti_settings[chat_id]["antitext"] else "OFF", callback_data="antitext_off")
    )
    bot.reply_to(msg, "Atur *Antitext*:", reply_markup=kb, parse_mode="Markdown")

# ========== CALLBACK TOGGLE ==========
@bot.callback_query_handler(func=lambda call: call.data in [
    "antilink_on","antilink_off",
    "antiforward_on","antiforward_off",
    "antitext_on","antitext_off"
])
def toggle_proteksi(call):
    chat_id = call.message.chat.id
    ensure_chat_settings(chat_id)
    fitur = call.data.split("_")[0]
    anti_settings[chat_id][fitur] = call.data.endswith("on")

    # kasih notifikasi singkat di atas tombol
    bot.answer_callback_query(
        call.id,
        f"{fitur.upper()} {'ON' if anti_settings[chat_id][fitur] else 'OFF'}"
    )

    # hapus pesan tombol setelah dipencet
    try:
        bot.delete_message(chat_id, call.message.message_id)
    except:
        pass

# ========== ADD BLACKLIST ==========
@bot.message_handler(commands=command_case_insensitive(["addantitext"]))
@require_host_or_admin
def add_antitext(msg):
    chat_id = msg.chat.id
    ensure_chat_settings(chat_id)
    if not is_admin(chat_id, msg.from_user.id):
        bot.reply_to(msg, "Hanya admin yang bisa menambah blacklist.")
        return
    m = bot.reply_to(msg, "✍️ Kirim kata yang mau di-blacklist (pisahkan dengan koma):")
    bot.register_next_step_handler(m, process_add_antitext)

def process_add_antitext(msg):
    chat_id = msg.chat.id
    ensure_chat_settings(chat_id)
    kata = [k.strip().lower() for k in msg.text.split(",") if k.strip()]
    if not kata:
        bot.reply_to(msg, "⚠️ Tidak ada kata valid.")
        return
    for k in kata:
        if k not in anti_settings[chat_id]["blacklist"]:
            anti_settings[chat_id]["blacklist"].append(k)
    bot.reply_to(msg, f"Ditambahkan ke blacklist:\n`{', '.join(kata)}`", parse_mode="Markdown")

# ========== LIST BLACKLIST ==========
@bot.message_handler(commands=command_case_insensitive(["listantitext"]))
@require_host_or_admin
def list_antitext(msg):
    chat_id = msg.chat.id
    ensure_chat_settings(chat_id)
    daftar = anti_settings[chat_id]["blacklist"]
    if not daftar:
        bot.reply_to(msg, "⚠️ Blacklist kosong.")
        return
    teks = "📋 *Daftar Blacklist:*\n" + "\n".join([f"- {k}" for k in daftar])
    bot.reply_to(msg, teks, parse_mode="Markdown")

# ========== DELANTITEXT ==========
@bot.message_handler(commands=command_case_insensitive(["delantitext"]))
@require_host_or_admin
def delantitext_menu(msg):
    chat_id = msg.chat.id
    ensure_chat_settings(chat_id)
    if not is_admin(chat_id, msg.from_user.id):
        bot.reply_to(msg, "Hanya admin yang bisa hapus blacklist.")
        return
    daftar = anti_settings[chat_id]["blacklist"]
    if not daftar:
        bot.reply_to(msg, "⚠️ Belum ada kata blacklist.")
        return
    kb = types.InlineKeyboardMarkup()
    for kata in daftar:
        kb.add(types.InlineKeyboardButton(f"{kata}", callback_data=f"delbl_{kata}"))
    kb.add(types.InlineKeyboardButton("Done", callback_data="done_del"))
    bot.reply_to(msg, "?? *PILIH YANG MAU DIHAPUS*", reply_markup=kb, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delbl_"))
def del_blacklist_cb(call):
    chat_id = call.message.chat.id
    kata = call.data.replace("delbl_", "")
    if kata in anti_settings[chat_id]["blacklist"]:
        anti_settings[chat_id]["blacklist"].remove(kata)
        bot.answer_callback_query(call.id, f"{kata} dihapus")
    daftar = anti_settings[chat_id]["blacklist"]
    if daftar:
        kb = types.InlineKeyboardMarkup()
        for k in daftar:
            kb.add(types.InlineKeyboardButton(f"{k}", callback_data=f"delbl_{k}"))
        kb.add(types.InlineKeyboardButton("Done", callback_data="done_del"))
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=kb)
    else:
        bot.edit_message_text("Semua blacklist sudah dihapus", chat_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "done_del")
def done_delete(call):
    bot.edit_message_text("Selesai hapus blacklist", call.message.chat.id, call.message.message_id)
    
# ===========================
# BROADCAST
# ===========================
@bot.message_handler(commands=["bc"])
def handle_bc_grup(message):
    if message.from_user.id != OWNER_USER_ID:
        return bot.reply_to(message, "❌ Hanya owner yang bisa memakai broadcast.")

    sent = 0
    fail = 0

    # =============================
    # AMBIL SEMUA GRUP TARGET
    # =============================
    all_groups = set(list(sewabot_map.keys()) + [str(x) for x in allowed_groups])

    # =============================
    # CEK JIKA BROADCAST FOTO
    # =============================
    if message.reply_to_message and message.reply_to_message.photo:
        photo_id = message.reply_to_message.photo[-1].file_id

        # caption setelah command /bcgrup
        parts = message.text.split(maxsplit=1)
        extra_caption = parts[1] if len(parts) > 1 else ""

        # caption foto asli + caption tambahan
        base_caption = message.reply_to_message.caption or ""
        final_caption = (base_caption + "\n" + extra_caption).strip()

        for gid in all_groups:
            try:
                bot.send_photo(int(gid), photo_id, caption=final_caption)
                sent += 1
            except:
                fail += 1

        return bot.reply_to(
            message,
            f"📸 Broadcast FOTO + TEKS selesai!\n\n"
            f"✔️ Sukses: {sent}\n❌ Gagal: {fail}"
        )

    # =============================
    # BROADCAST TEKS SAJA
    # =============================
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.reply_to(
            message,
            "Gunakan:\n"
            "/bcgrup teks...\n"
            "Atau reply foto → /bcgrup caption tambahan"
        )

    text = parts[1]

    for gid in all_groups:
        try:
            bot.send_message(int(gid), text)
            sent += 1
        except:
            fail += 1

    bot.reply_to(
        message,
        f"📢 Broadcast TEKS selesai!\n\n"
        f"✔️ Sukses: {sent}\n❌ Gagal: {fail}"
    )

# ===========================
# TAG ADMIN
# ===========================
@bot.message_handler(commands=['tagalladmin'])
def tag_admins(message):
    if message.chat.type not in ["group", "supergroup"]:
        return bot.reply_to(message, "Perintah ini hanya di grup.")

    admins = bot.get_chat_administrators(message.chat.id)
    mentions = []

    for admin in admins:
        user = admin.user
        if user.username:
            mentions.append("@" + user.username)
        else:
            mentions.append(f"<a href=\"tg://user?id={user.id}\">{user.first_name}</a>")

    bot.send_message(message.chat.id, "\n".join(mentions), parse_mode="HTML")

# ===========================
# TAGALL FINAL
# ===========================
@bot.message_handler(commands=['tagall'])
def tag_all(message):
    if message.chat.type not in ["group", "supergroup"]:
        return bot.reply_to(message, "Perintah ini hanya untuk grup.")

    if message.chat.id not in seen_members:
        return bot.reply_to(message, "Belum ada member yang terekam.")

    mentions = []
    for uid, name in seen_members[message.chat.id].items():
        mentions.append(f"<a href=\"tg://user?id={uid}\">{name}</a>")

    for chunk in chunk_list(mentions, 15):
        teks = "🔔 *TAGALL:*\n" + "\n".join(chunk)
        bot.send_message(message.chat.id, teks, parse_mode="HTML")  
        
@bot.message_handler(commands=["addfilterg"])
def add_filter_grup(message):
    chat_id = str(message.chat.id)

    # Hanya admin grup atau owner
    member = bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ["creator", "administrator"] and message.from_user.id != OWNER_USER_ID:
        return bot.reply_to(message, "❌ Hanya admin/owner yang bisa menambah filter.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.reply_to(message, "Gunakan: /addfilterg kata")

    word = parts[1].lower().strip()

    if chat_id not in group_filters:
        group_filters[chat_id] = []

    if word not in group_filters[chat_id]:
        group_filters[chat_id].append(word)
        save_group_filters()

    bot.reply_to(message, f"✅ Filter ditambahkan untuk grup ini:\n`{word}`", parse_mode="Markdown")
            
@bot.message_handler(commands=["delfilterg"])
def del_filter_grup(message):
    chat_id = str(message.chat.id)

    member = bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ["creator", "administrator"] and message.from_user.id != OWNER_USER_ID:
        return bot.reply_to(message, "❌ Hanya admin/owner yang bisa menghapus filter.")

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.reply_to(message, "Gunakan: /delfilterg kata")

    word = parts[1].lower().strip()

    if chat_id in group_filters and word in group_filters[chat_id]:
        group_filters[chat_id].remove(word)
        save_group_filters()
        return bot.reply_to(message, f"🗑 Filter dihapus:\n`{word}`", parse_mode="Markdown")

    bot.reply_to(message, "❌ Kata itu tidak ada di filter grup ini.")
 
@bot.message_handler(func=lambda m: m.chat.type in ["group", "supergroup"])
def auto_filter_per_group(message):
    chat_id = str(message.chat.id)

    if chat_id not in group_filters:
        return  # grup belum punya filter

    if not message.text:
        return  # pesan tanpa teks (sticker, foto tanpa caption)

    text = message.text.lower()

    for bad in group_filters[chat_id]:
        if bad in text:
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except:
                pass
            return  # stop setelah menghapus
                                            
        
# =============== HANDLER BRPADCAST =====================#
# Menyimpan user yang pernah chat bot (untuk broadcast user)
user_list_file = "user_list.txt"
user_list = set()

def load_user_list():
    try:
        with open(user_list_file) as f:
            for line in f:
                user_list.add(int(line.strip()))
    except:
        pass

def save_user(user_id):
    if user_id not in user_list:
        user_list.add(user_id)
        with open(user_list_file, "a") as f:
            f.write(str(user_id) + "\n")

load_user_list()

# TRACK PRIVATE USER TANPA MENGGANGGU HANDLER LAIN
@bot.message_handler(func=lambda m: m.chat.type == "private" and not (m.text and m.text.startswith('/')))
def track_user(m):
    save_user(m.from_user.id)


@bot.message_handler(func=lambda m: m.chat.type in ["group", "supergroup"])
def track_group(m):
    group_data.add(m.chat.id)
           
# ================================
#  SISTEM TAGADMIN & TAGALL FINAL
# ================================
from collections import defaultdict, OrderedDict

seen_members = defaultdict(OrderedDict)

@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'])
def track_group_activity(m):
    try:
        chat_id = str(m.chat.id)
        uid = m.from_user.id

        if m.from_user.username:
            name = f"@{m.from_user.username}"
        else:
            name = m.from_user.first_name or "User"

        if chat_id not in seen_members:
            seen_members[chat_id] = OrderedDict()

        if uid in seen_members[chat_id]:
            seen_members[chat_id].pop(uid)

        seen_members[chat_id][uid] = name

        if len(seen_members[chat_id]) > 600:
            seen_members[chat_id].popitem(last=False)

    except:
        pass


def chunk_list(lst, n):
    """Split list into chunks of n."""
    for i in range(0, len(lst), n):
        yield lst[i:i+n]
                         
# ========== HANDLER PROTEKSI ==========
@bot.message_handler(func=lambda m: True, content_types=[
    "text","photo","video","document","sticker","audio","voice","animation","poll"
])
def proteksi(msg):
    try:
        chat_id = msg.chat.id
        user_id = msg.from_user.id
        ensure_chat_settings(chat_id)

        if user_id == OWNER_ID:  # owner kebal
            return
        if msg.chat.type not in ["group","supergroup"]:
            return

        set = anti_settings[chat_id]

        # === ANTIFORWARD ===
        if (msg.forward_from or msg.forward_from_chat or getattr(msg, "is_automatic_forward", False)):
            if set["antiforward"]:
                bot.delete_message(chat_id, msg.message_id)
                return

        # === ANTILINK ===
        if msg.content_type == "text":
            teks = msg.text.lower()
            if set["antilink"] and ("http://" in teks or "https://" in teks or "t.me/" in teks):
                bot.delete_message(chat_id, msg.message_id)
                return

            # === ANTITEXT ===
            if set["antitext"]:
                if set["blacklist"]:
                    for k in set["blacklist"]:
                        if k in teks:
                            bot.delete_message(chat_id, msg.message_id)
                            return
                else:  # kalau blacklist kosong hapus semua teks
                    bot.delete_message(chat_id, msg.message_id)
                    return
    except Exception as e:
        print("Error proteksi:", e)
        
# =============== HANDLER BRPADCAST =====================#
@bot.message_handler(func=lambda m: True)
def track_user(m):
    if m.chat.type == "private":
        save_user(m.from_user.id)              

# =========================
# Jalankan Bot Asyncio
# =========================
if __name__ == "__main__":
    import threading

    # jalanin cek expired di background
    threading.Thread(
    target=check_payment_loop,
    daemon=True
).start()

    async def main():
        await bot.infinity_polling(
            timeout=20,
            skip_pending=True,
            allowed_updates=["message", "callback_query", "chat_member"]
        )

    import asyncio
    asyncio.run(main())