"""Регистратор хендлеров: порядок импортов = порядок приоритета в aiogram.

Правило aiogram: «первый подходящий хендлер выигрывает».
Поэтому модули подключены строго в том порядке, в котором хендлеры
были объявлены в старом едином файле handlers.py:
команды → игры → перехват (catch-all последним в бизнес-чатах) → ИИ → меню → админка.
"""
import handlers_gate       # 🛡 гейт подписки (первым: middleware перехватывают всех до остальных)
import handlers_start      # /start, /admin, business_connection (кэш владельца)
import handlers_commands   # .spam .mute .nomute .afk .code .wbl .ai .price .curs + форматирование
import handlers_sled       # 🛰 .sled .unsled .infosled — отслеживание профиля
import handlers_games      # .knb (Камень·Ножницы·Бумага)
import handlers_level      # ⬆ .level .level top .unlevel — XP и уровни за активность
import handlers_ramka      # .ramka (золотая рамка на фото)
import handlers_stik       # 🏷 .stik (фото → стикер)
import handlers_krom       # 🎥 .krom (видео → кружок)
import handlers_voice       # 🎙 .voice (текст → голосовое)
import handlers_wm         # 🏷 .wm (водяной знак на фото)
import handlers_gif       # 🎞 .gif (видео → гифка)
import handlers_img       # 🎨 .img (ИИ-генерация картинок по тексту)
import handlers_intercept  # перехват удалённых/изменённых + голосовые (catch-all)
import handlers_ai         # ИИ-консьерж в ЛС и поиск по архиву
import handlers_menu       # ЛС-меню: сохранённые, howto, профиль, архив, донаты
import handlers_admin      # админка, .cmd список команд, рассылки, группы/каналы
