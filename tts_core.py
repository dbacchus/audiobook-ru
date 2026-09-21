# -*- coding: utf-8 -*-
"""
Ядро озвучки: текст -> ударения -> аудио. Без графики, чтобы этим же модулем
можно было пользоваться из командной строки и из приложения.

Цепочка:
    1) RUAccent ставит ударения по контексту (омографы разбирает нейросеть);
    2) пользовательский словарь применяется ПОВЕРХ (он всегда побеждает);
    3) правила односложных слов: служебные без "+", знаменательные с "+",
       многосложные без знака получают ударение от RUAccent отдельным запросом;
    4) Silero TTS читает готовую разметку (put_accent=False).

Наследует все решения, найденные на слух при озвучке повести «Порог»
(сентябрь 2026): модель v5_5_ru, SSML с разгоном 300 мс в начале фрагмента,
заголовки медленно и низко, ffmpeg loudnorm -18 LUFS.

Требуется: torch, torchaudio, omegaconf, numpy, ruaccent, imageio-ffmpeg.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import wave

# Консоль Windows по умолчанию cp1252: печать кириллицы роняет скрипт.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

VERSION = "1.5"
SAMPLE_RATE = 48000
# Предел у модели не по символам, а по длительности генерации: на «Пороге»
# абзац в 889 знаков падал с «Model couldn't generate your text, probably it's
# too long», а соседний в 897 проходил. Режем с запасом; если всё же упрёмся,
# Synth.say делит фрагмент пополам и читает по частям (см. там же).
MAX_CHARS = 750
LEADIN_MS = 300           # пауза-разгон в начале фрагмента (иначе съедается первая согласная)
# Модель по умолчанию -- v5_cis_base: она под MIT (специализированные русские
# модели Silero под CC BY-NC, только некоммерческое использование), в ней
# 29 русских голосов вместо пяти, и она РАССЧИТАНА на текст с проставленными
# ударениями («no auto-stress or homographs») -- то есть ровно на нашу схему,
# где ударения ставит RUAccent.
DEFAULT_MODEL = "v5_cis_base"

MODELS = ["v5_cis_base", "v5_5_ru", "v4_ru", "v3_1_ru"]

VOICES_RU = {
    "aidar":   "Айдар — мужской",
    "eugene":  "Евгений — мужской",
    "baya":    "Бая — женский",
    "kseniya": "Ксения — женский",
    "xenia":   "Ксюша — женский",
}
# Пол указан только там, где он однозначен по имени; остальные подписаны
# именем -- проще один раз послушать, чем гадать.
VOICES_CIS = {
    "ru_alexandr": "Александр — мужской", "ru_bogdan": "Богдан — мужской",
    "ru_dmitriy": "Дмитрий — мужской", "ru_eduard": "Эдуард — мужской",
    "ru_igor": "Игорь — мужской", "ru_marat": "Марат — мужской",
    "ru_roman": "Роман — мужской", "ru_gamat": "Гамат — мужской",
    "ru_safarhuja": "Сафархуджа — мужской",
    "ru_albina": "Альбина — женский", "ru_aigul": "Айгуль — женский",
    "ru_alfia": "Альфия — женский", "ru_alfia2": "Альфия 2 — женский",
    "ru_ekaterina": "Екатерина — женский", "ru_karina": "Карина — женский",
    "ru_nurgul": "Нургуль — женский", "ru_oksana": "Оксана — женский",
    "ru_ramilia": "Рамиля — женский", "ru_saida": "Саида — женский",
    "ru_vika": "Вика — женский", "ru_zara": "Зара — женский",
    "ru_zhadyra": "Жадыра — женский", "ru_zhazira": "Жазира — женский",
    "ru_zinaida": "Зинаида — женский", "ru_kermen": "Кермен — женский",
    # У этих четверых Silero пол не указывает -- похоже, они из тюркской
    # части мультиязычного набора. На слух он слышен однозначно, так что
    # помечаем сами, по мере того как слушаем.
    "ru_kejilgan": "Кежилган — мужской, в возрасте", "ru_miyau": "Мияу",
    "ru_onaoy": "Онаой", "ru_sibday": "Сибдей",
}
VOICES_BY_MODEL = {
    "v5_cis_base": VOICES_CIS,
    "v5_5_ru": VOICES_RU, "v4_ru": VOICES_RU, "v3_1_ru": VOICES_RU,
}
VOICES = VOICES_CIS          # совместимость со старым обращением


def voices_for(model_id: str) -> dict:
    """Голоса модели. Списки зашиты, чтобы выбирать голос до её загрузки."""
    return VOICES_BY_MODEL.get(model_id, VOICES_RU)

# Фраза для пробы голоса -- уже размеченная: образец не должен зависеть от
# того, загрузился ли RUAccent, и заодно показывает, как выглядит разметка.
SAMPLE_PHRASE = ("В+етер к в+ечеру ст+их, и над вод+ой вст+ал тум+ан, "
                 "б+елый и пл+отный, как св+ежая изв+ёстка.")

VOWELS = "аеёиоуыэюя"
WORD_RE = re.compile(r"[А-Яа-яЁё+]+")
# Первое слово предложения: начало строки или после «.!?…», далее возможные
# тире, кавычки и скобки прямой речи. Шаблон ОДИН на оба прохода (снять
# регистр в исходнике, вернуть его в размеченном): проходы нумеруют начала
# предложений по порядку, и совпасть они могут только при одном шаблоне.
# Слово берём любое, вместе с возможным знаком ударения -- он может стоять
# и перед первой буквой («+это»).
SENTENCE_FIRST_WORD = re.compile(
    r'(^|[.!?…]["»\')\]]*\s+)([-—–«"\'(\[\s]*)(\+?[А-Яа-яЁё][А-Яа-яЁё+]*)')

# Дефис, приклеенный к предыдущему слову: так отличается постфикс «-то» в
# «что-то» от самостоятельного «то» после тире («сказал -- то ли правда»).
POSTFIX_TO = re.compile(r"[А-Яа-яЁё]-$")

# Уступительный оборот «бы (то) ни было»: ударение перетягивает частица «ни»,
# а глагол остаётся безударным -- «верить чему бы то н+и было», «как бы то н+и
# было», «во что бы то н+и стало». Это тот же сдвиг, что в «н+е был», «н+а
# воду»: в устойчивом обороте предлог или частица забирают ударение у
# знаменательного слова. Правило на слух 2026-09-20: пословная разметка давала
# «...ни б+ыло», и это слышно как ошибка.
# Только формы «быть»: на слух 2026-09-20 «во что бы то ни ст+ало» оказалось
# лучше, чем «н+и стало», -- сдвиг лексикализован не во всём обороте.
#
# Формы, на которые перенос идёт: был, было, были. Женский род ударение
# СОХРАНЯЕТ («не была́», «какова бы ни была́»), поэтому «была» сюда не входит,
# и хвост проверяется явно: обычная граница слова \b считает границей знак
# «+», так что «был+а» иначе подошло бы под шаблон.
_BYT_PAST = r"(б\+?ы(?:ло|ли|л))(?![+А-Яа-яЁё])"
CONCESSIVE = re.compile(r"\bбы(\s+т\+?о)?(\s+т\+?ам)?\s+ни\s+" + _BYT_PAST,
                        re.IGNORECASE)

# Перенос ударения на отрицание: «н+е был», «н+е было», «н+е были». Описан
# у Аванесова («Русское литературное произношение») вместе с «н+а воду» и
# «п+од руку», но на слух 2026-09-20 из всего семейства прижились только
# формы «быть»: у «не спал» перенос звучит хуже, у предлогов тоже. То есть
# та же лексикализация, что в обороте выше, и на тех же словах.
NEG_RETRACT = re.compile(r"\bне\s+" + _BYT_PAST, re.IGNORECASE)


def fix_neg_retract(m) -> str:
    """«не б+ыло» -> «н+е было»."""
    ne = m.group(0)[:2]          # «не» или «Не»: регистр сохраняем
    return f"{ne[0]}+{ne[1]} {m.group(1).replace('+', '')}"


def fix_concessive(m) -> str:
    """«бы то ни б+ыло» -> «бы то н+и было»."""
    tail = "".join(g or "" for g in m.group(1, 2)).replace("+", "")
    return f"бы{tail} н+и {m.group(3).replace('+', '')}"


# Безударный постфикс через дефис: Silero читает кусок после дефиса отдельным
# словом, и «чт+о-то» звучит как «что та» (на слух 2026-09-20, три фразы из
# трёх). Склеиваем -- «чт+ото» читается одним словом с правильной редукцией.
# Только там, где после дефиса НЕТ своего ударения: «ч+ём-ниб+удь»,
# «по-ст+арому», «м+атово-с+ерым» разницы не дали и остаются как есть.
# Хвост проверяем явным «дальше не буква и не знак ударения»: обычная граница
# слова \b здесь обманывает -- «+» для неё граница, и «м+атово-с+ерым» ловилось
# как постфикс «-с».
CLITIC_HYPHEN = re.compile(r"(?<=[А-Яа-яЁё])-(то|ка|таки)(?![+А-Яа-яЁё])",
                           re.IGNORECASE)


def glue_clitics(text: str) -> str:
    """Убрать дефис перед безударным постфиксом -- только для синтеза.

    В .acc.txt дефис остаётся: его читает и правит человек, и «чт+ото» там
    выглядело бы опечаткой."""
    return CLITIC_HYPHEN.sub(r"\1", text)

# Односложные служебные слова: RUAccent помечает их "+", а Silero читает
# помеченное слово как выделенное («ЧТО они научатся» вместо «что они НАУЧАТСЯ»).
FUNCTION_WORDS = set("""
в во к ко с со у о об от до за на по из над под при про для без сквозь чрез через
и а но да же ли ль бы б уж ведь ну вот вон ни не чтоб хоть
я ты мы вы их им ей ему её его нас вас нам вам ней них ним
""".split())
# «он» отсюда УБРАН (на слух 2026-09-21, голосом Ани): без знака он
# редуцируется в «ан» -- «Ан приедет». Раньше проверяли голосом рассказчика
# и разницы не слышали, а у каждого персонажа голос свой, и редукция от него
# зависит; со знаком стало заметно лучше у Ани и не хуже у рассказчика.
# Заодно это убрало давнюю непоследовательность: «он+а», «он+о», «он+и»
# ударение получали всегда (они двусложные), и только «он» стоял без знака
# за одну свою краткость. То же с «ег+о», «ем+у», «е+ё» -- помечены, а
# односложные «их», «им», «ей» пока нет: их ещё не проверяли.
# «то» здесь БЫЛО и звучало как «та»: безударная «о» по-русски и есть [а], а
# отдельное «то» -- слово ударное во всех значениях («и то, как она
# заполнялась», «и то и другое», «в то лето», «если лёд, то каждые полчаса»;
# все четыре проверены на слух 2026-09-19). Служебное оно только постфиксом,
# «что-то», «где-то», «какой-то», и этот случай ловится отдельно в
# fix_monosyllables: WORD_RE не берёт дефис, поэтому «что-то» приходит туда
# двумя словами и отличить постфикс можно только по дефису слева.
# «что», «как», «где», «кто», «чем» -- НЕ служебные: это местоимения и наречия,
# и ударение у них обычное. Правка 2026-09-17 после прослушивания: без знака
# они звучат вязко («Мы боялись, что машины научатся думать»). Интонационное
# выделение в прямом вопросе -- отдельная и гораздо более сильная штука,
# ею занимается EMPHASIS в SSML, а не знак ударения.
INTERROGATIVES = {"что", "как", "чем", "где", "кто", "куда", "когда", "зачем",
                  "почему", "сколько", "чей", "чья", "чьё", "чьи", "какой",
                  "какая", "какое", "какие"}

# Слова, которые в русском различаются ТОЛЬКО ударением. Подсвечиваются в
# редакторе: именно здесь автомат может выбрать не тот смысл, всё остальное
# он ставит однозначно. Список рабочий, а не исчерпывающий.
HOMOGRAPHS = set("""
замок замка замки замком атлас ирис орган органа органы хлопок
кружки кружка белки белка белок мука муки дорога дороги дорогой
стоит стоят уже потом большая большие село села сёла полки полка
стрелки стрелок пропасть вести жаркое духи парить плачу полно пили
пора пристань рожки сорок трусить чудно видение выходить засыпать
насыпать мели стоящий хаос стороны голоса дома руки ноги воды горы
земли окна стены слова леса снега моря поля берега вечера ветра
города острова холода цвета счета корпуса ордена колокола купола
знаком верхом кругом бегом порой косой весло вёсла целую пахнет
толку топи торги травы тропы трубы утра хором цепи часа чашки
числа шага шелка шкуры якоря пути пруды разрез проклятый
""".split())

# ---------------------------------------------------------------- ffmpeg


# Слова, где написание без «ё» тоже законно, то есть выбор е/ё решает СМЫСЛ, а не
# орфография: «все были» / «всё было», «он берет» / «он берёт». RUAccent выбирает
# их отдельной моделью по контексту (yo_homograph_model), и именно эти места
# имеет смысл просматривать глазами. Слова вида «еще», «идет» сюда не входят:
# там «ё» однозначно и автомат его просто восстанавливает.
YO_AMBIGUOUS = set("""
все всем всех всей всею
берет берете берем шлем поем поет поете узнаем узнает узнаете
небо неба небу небе небом осел слез слезы слезу мел щеки
падеж падежа заем займа наем найма чем причем железа
совершенный совершенное совершенные совершенно крестный крестная крестного
истекший истекшие оглашенный оглашенные
""".split())


def load_yo_list(path: str) -> int:
    """Пополнить YO_AMBIGUOUS сохранённым списком спорных «е»/«ё» слов.

    Список берётся из самого RUAccent (`yo_homographs`, 637 слов -- ровно те,
    где выбор буквы делает его модель) и кладётся рядом с приложением, чтобы
    подсветка работала и до загрузки моделей."""
    try:
        with open(path, encoding="utf-8") as f:
            words = json.load(f)
    except Exception:
        return 0
    YO_AMBIGUOUS.update(w.lower() for w in words if w)
    return len(words)


def save_yo_list(path: str, words) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(words), f, ensure_ascii=False)
    except Exception:
        pass


def app_root() -> str:
    """Папка приложения: рядом с exe в собранной версии, рядом с кодом в обычной."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_model_file(model_id: str) -> str | None:
    """Файл модели Silero: сначала рядом с приложением (папка models), затем в
    кэше torch.hub. Прямая загрузка из .pt не требует ни интернета, ни
    GitHub и занимает доли секунды вместо нескольких секунд через hub."""
    import glob
    places = [os.path.join(app_root(), "models", model_id + ".pt"),
              os.path.join(app_root(), model_id + ".pt")]
    for p in places:
        if os.path.exists(p):
            return p
    pat = os.path.expanduser(f"~/.cache/torch/hub/**/{model_id}.pt")
    found = glob.glob(pat, recursive=True)
    return found[0] if found else None


def short_path(path: str) -> str:
    """Короткое 8.3-имя Windows: «...\\Проверка пути\\Аудиокнига» ->
    «...\\EC86~1\\4C0E~1». Нужно там, где путь уходит в сишную библиотеку.
    Возвращает исходный путь, если короткие имена недоступны."""
    if os.name != "nt" or not path:
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(4096)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 4096)
        return buf.value if n and buf.value else path
    except Exception:
        return path


def patch_pycrfsuite_paths() -> None:
    """Морфология внутри RUAccent (pycrfsuite) открывает свою модель сишной
    функцией в системной кодировке: из папки с кириллицей она сообщает «Error
    opening model file» о файле, который есть. Путь берётся от расположения
    модуля, подменить его нельзя, поэтому подменяем сам Tagger: он получает
    то же место, названное короткими латинскими именами."""
    try:
        import pycrfsuite
    except Exception:
        return
    if getattr(pycrfsuite.Tagger, "_audiobook_patched", False):
        return
    base = pycrfsuite.Tagger

    class Tagger(base):
        _audiobook_patched = True

        def open(self, name, *args, **kwargs):
            if not str(name).isascii():
                name = short_path(str(name))
            return super().open(name, *args, **kwargs)

    pycrfsuite.Tagger = Tagger


def ruaccent_workdir() -> str | None:
    """Папка с моделями RUAccent рядом с приложением, если она есть."""
    d = os.path.join(app_root(), "models", "ruaccent")
    return d if os.path.isdir(os.path.join(d, "nn")) else None


# Готовый ffmpeg мы не распространяем: сборки, которые лежат в пакетах,
# собраны с --enable-gpl. Вместо этого предлагаем скачать официальную
# LGPL-сборку -- 73 МБ, распаковывается в папку ffmpeg рядом с приложением.
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-lgpl-shared.zip")
FFMPEG_SIZE_MB = 73


def ffmpeg_exe() -> str:
    """ffmpeg рядом с приложением, из PATH или из пакета imageio-ffmpeg."""
    import shutil
    for p in (os.path.join(app_root(), "ffmpeg.exe"),
              os.path.join(app_root(), "ffmpeg", "ffmpeg.exe")):
        if os.path.exists(p):
            return p
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def have_ffmpeg() -> bool:
    exe = ffmpeg_exe()
    return bool(exe) and (os.path.exists(exe) or os.path.isabs(exe) is False)


def refresh_ffmpeg() -> str:
    """Пересчитать путь (после того как ffmpeg скачали)."""
    global FFMPEG
    FFMPEG = ffmpeg_exe()
    return FFMPEG


def download_ffmpeg(progress=None, log=print, cancel=None) -> str:
    """Скачать официальную LGPL-сборку и распаковать рядом с приложением.

    Кладём ffmpeg.exe и его библиотеки в папку `ffmpeg`; ffplay и ffprobe
    не нужны и пропускаются. Возвращает путь к ffmpeg.exe или пустую строку."""
    import io
    import urllib.request
    import zipfile

    dest = os.path.join(app_root(), "ffmpeg")
    os.makedirs(dest, exist_ok=True)
    log(f"Скачиваю ffmpeg ({FFMPEG_SIZE_MB} МБ) с github.com/BtbN/FFmpeg-Builds …")
    buf = io.BytesIO()
    req = urllib.request.Request(FFMPEG_URL, headers={"User-Agent": "audiobook/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length", 0)) or FFMPEG_SIZE_MB * 1024 * 1024
        got = 0
        while True:
            if cancel is not None and cancel.is_set():
                return ""
            chunk = r.read(1 << 20)
            if not chunk:
                break
            buf.write(chunk)
            got += len(chunk)
            if progress:
                progress(got // (1 << 20), total // (1 << 20), "ffmpeg")
    buf.seek(0)
    log("Распаковываю …")
    with zipfile.ZipFile(buf) as z:
        for name in z.namelist():
            base = os.path.basename(name)
            if "/bin/" not in name or not base:
                continue
            if base != "ffmpeg.exe" and not base.endswith(".dll"):
                continue          # ffplay и ffprobe не нужны
            with z.open(name) as src, open(os.path.join(dest, base), "wb") as out:
                shutil_copy(src, out)
    exe = os.path.join(dest, "ffmpeg.exe")
    if os.path.exists(exe):
        refresh_ffmpeg()
        log(f"ffmpeg готов: {exe}")
        return exe
    log("Не получилось: в архиве не нашёлся ffmpeg.exe")
    return ""


def shutil_copy(src, dst, size=1 << 20):
    while chunk := src.read(size):
        dst.write(chunk)


FFMPEG = ffmpeg_exe()

# Под pythonw у приложения нет консоли, поэтому каждый запуск ffmpeg открывал
# бы своё чёрное окно. CREATE_NO_WINDOW их прячет; вывод перехватываем, чтобы
# жалобы ffmpeg попадали в журнал, а не в никуда.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def run_quiet(cmd, **kw):
    return subprocess.run(cmd, check=True, creationflags=_NO_WINDOW,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)

# ---------------------------------------------------------------- разбор текста

BREAK_MARKERS = {"***", "* * *", "---", "— — —"}
CHAPTER_WORDS = ("Глава", "Часть", "Интерлюдия", "Пролог", "Эпилог")
# что начинает НОВЫЙ файл при делении по главам; «Часть» не начинает --
# её заголовок присоединяется к следующей главе
CHAPTER_STARTS = ("Глава", "Интерлюдия", "Пролог", "Эпилог")
MAX_EPIGRAPH_PARS = 4

# Главы часто пишут в markdown. Сама модель незнакомые символы выбрасывает,
# но разметка мешает делению на предложения, поэтому снимаем её при разборе
# (в файле с ударениями она остаётся: там важно сохранить исходный вид).
MD_RULES = [
    (re.compile(r"^\s{0,3}>\s?"), ""),                       # цитата
    (re.compile(r"^\s{0,3}[-*+]\s+"), ""),                   # маркер списка
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),             # **жирный**
    (re.compile(r"__(.+?)__", re.S), r"\1"),                 # __жирный__
    (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S), r"\1"),   # *курсив*
    (re.compile(r"`([^`]*)`"), r"\1"),                       # `код`
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),         # ссылка
]


def strip_markdown(s: str) -> str:
    for rx, rep in MD_RULES:
        s = rx.sub(rep, s)
    return s.strip()


def parse_text(text: str, epigraph: bool = True, split_chapters: bool = False):
    """Текст -> список секций [(заголовок, [(вид, текст), ...]), ...].

    Виды: title (заголовок), par (абзац), epigraph, attrib (подпись под
    эпиграфом), brk (пауза смены сцены).
        "# что-то"   -> заголовок
        "***"        -> пауза
        прочее       -> абзац
    split_chapters=True: каждый "# Глава ..." начинает НОВУЮ секцию (отдельный
    файл на выходе); иначе весь текстовый файл -- одна секция.
    """
    sections, cur, cur_title = [], [], None
    # Эпиграф -- короткий блок перед первой «Главой»/«Частью», его читают
    # медленнее. Опознаём его осторожно: только если такой заголовок в файле
    # вообще есть, и не длиннее четырёх абзацев. Иначе глава без заголовков
    # целиком уехала бы в замедленное чтение.
    lines_ = text.splitlines()
    # заголовки сравниваем БЕЗ знаков ударения: в файле с ударениями они
    # выглядят как «Гл+ава п+ервая», и проверка на «Глава» иначе не срабатывает
    has_chapter = any(l.lstrip().lstrip("#").replace("+", "").strip()
                      .startswith(CHAPTER_WORDS)
                      for l in lines_ if l.lstrip().startswith("#"))
    pre_chapter = epigraph and has_chapter
    n_epi = 0
    pending = None                 # заголовок «Часть», ждущий свою главу
    buf = []                       # строки одного абзаца
    # В тексте, перенесённом по ширине, строки внутри абзаца примерно равной
    # длины, а последняя короче. По этому и отличаем конец абзаца от переноса.
    widths = sorted(len(l.rstrip()) for l in lines_ if l.strip())
    wrap_width = widths[len(widths) // 2] if widths else 0
    short_line = wrap_width * 0.75 if wrap_width >= 40 else 0

    def flush():
        """Сложить накопленные строки в один абзац.

        Текст часто перенесён по ширине страницы, и тогда каждая строка --
        не абзац, а его кусок. Если считать строки абзацами, чтец делает
        паузу посреди предложения. Абзац кончается пустой строкой,
        заголовком или разделителем сцены."""
        nonlocal n_epi, pre_chapter
        if not buf:
            return
        s = strip_markdown(" ".join(buf))
        buf.clear()
        if not s:
            return
        if pre_chapter:
            n_epi += 1
            if n_epi > MAX_EPIGRAPH_PARS:
                pre_chapter = False
        cur.append(("epigraph" if pre_chapter else "par", s))

    for raw in lines_:
        s = raw.strip()
        if not s:
            flush()
            continue
        if s.startswith("#"):
            flush()
            title = strip_markdown(s.lstrip("#").strip())
            if not title:
                continue
            plain = title.replace("+", "")
            if plain.startswith(CHAPTER_WORDS):
                pre_chapter = False
            if split_chapters and plain.startswith(CHAPTER_STARTS):
                if cur and cur_title:
                    sections.append((cur_title, cur))
                    cur = []
                cur_title = title
                if pending is not None:        # «Часть ...» звучит перед главой
                    cur.append(("title", pending))
                    pending = None
            elif split_chapters and plain.startswith("Часть"):
                pending = title
                continue
            elif cur_title is None:
                cur_title = title
            cur.append(("title", title))
        elif s in BREAK_MARKERS:
            flush()
            cur.append(("brk", None))
        else:
            buf.append(s)
            if short_line and len(s) < short_line:
                flush()            # короткая строка -- конец абзаца, а не перенос
    flush()
    if cur:
        sections.append((cur_title or "Без названия", cur))

    # подпись под эпиграфом: последний эпиграфный абзац, если он в кавычках
    if sections and epigraph:
        items = sections[0][1]
        epi = [i for i, (k, _) in enumerate(items) if k == "epigraph"]
        if epi and items[epi[-1]][1].lstrip().startswith(("«", '"')):
            items[epi[-1]] = ("attrib", items[epi[-1]][1])
    return sections


def split_long(par: str, limit: int = MAX_CHARS):
    """Режем абзац по предложениям на куски не длиннее limit."""
    if len(par) <= limit:
        return [par]
    sents = re.split(r"(?<=[.!?…])\s+", par)
    chunks, cur = [], ""
    for s in sents:
        if len(cur) + len(s) + 1 > limit and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- ударения


# ---------------------------------------------------------------- латиница
# В алфавите Silero латинских букв нет, и её разбор SSML на них ломается:
# «Интерлюдия I» -> «Failed to parse SSML: 'NoneType' object has no attribute
# 'keys'» (2026-09-17). Римские цифры при заголовках разворачиваем в слова,
# остальную латиницу переводим в кириллицу по буквам. Делается это на этапе
# расстановки ударений, чтобы замена была ВИДНА в файле с ударениями и её
# можно было поправить руками.

ROMAN_RE = re.compile(r"(?<![A-Za-zА-Яа-яЁё])"
                      r"(?=[IVXLC]{1,6}(?![A-Za-zА-Яа-яЁё]))"
                      r"(C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})"
                      r"(?![A-Za-zА-Яа-яЁё])")
_ROMAN_VAL = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}

ORDINALS = {
    "m": ["первый", "второй", "третий", "четвёртый", "пятый", "шестой", "седьмой",
          "восьмой", "девятый", "десятый", "одиннадцатый", "двенадцатый",
          "тринадцатый", "четырнадцатый", "пятнадцатый", "шестнадцатый",
          "семнадцатый", "восемнадцатый", "девятнадцатый", "двадцатый"],
    "f": ["первая", "вторая", "третья", "четвёртая", "пятая", "шестая", "седьмая",
          "восьмая", "девятая", "десятая", "одиннадцатая", "двенадцатая",
          "тринадцатая", "четырнадцатая", "пятнадцатая", "шестнадцатая",
          "семнадцатая", "восемнадцатая", "девятнадцатая", "двадцатая"],
    "n": ["первое", "второе", "третье", "четвёртое", "пятое", "шестое", "седьмое",
          "восьмое", "девятое", "десятое", "одиннадцатое", "двенадцатое",
          "тринадцатое", "четырнадцатое", "пятнадцатое", "шестнадцатое",
          "семнадцатое", "восемнадцатое", "девятнадцатое", "двадцатое"],
}
# род берём по слову перед цифрой: «Интерлюдия I» -> «Интерлюдия первая»
GENDER = {"часть": "f", "глава": "f", "книга": "f", "интерлюдия": "f",
          "сцена": "f", "песнь": "f", "тетрадь": "f", "серия": "f",
          "акт": "m", "том": "m", "раздел": "m", "пролог": "m", "эпилог": "m",
          "эпизод": "m", "день": "m", "год": "m",
          "действие": "n", "письмо": "n", "приложение": "n"}

# Латиницу приходится записывать русскими буквами, иначе модель её выбросит.
# Побуквенный транслит даёт «м+аркдовн» и «скреенсх+от», поэтому три уровня:
# знакомые слова из словаря, аббревиатуры по названиям букв, остальное --
# по правилам чтения (буквосочетания важнее отдельных букв).

LATIN_WORDS = {
    # то, что чаще всего встречается в текстах про эту самую программу
    "windows": "в+индоус", "python": "п+итон", "silero": "сил+еро",
    "github": "гитх+аб", "git": "гит", "markdown": "маркд+аун",
    "readme": "р+идми", "ffmpeg": "эф эф эм п+эг", "torch": "торч",
    "pytorch": "пайт+орч", "onnx": "+оникс", "runtime": "р+антайм",
    "transformers": "трансф+ормерс", "release": "рил+из", "releases": "рил+изес",
    "download": "даунл+оуд", "setup": "с+етап", "zip": "зип", "exe": "экз+е",
    "chrome": "хром", "intel": "инт+ел", "email": "им+ейл", "mail": "мейл",
    "online": "онл+айн", "offline": "офл+айн", "software": "с+офтвер",
    "hardware": "х+ардвер", "internet": "интерн+ет", "web": "веб",
    "site": "сайт", "server": "с+ервер", "cloud": "кл+ауд", "file": "файл",
    "code": "коуд", "open": "+оупен", "source": "сорс", "license": "л+айсенс",
    "apache": "ап+ач", "linux": "л+инукс", "microsoft": "м+айкрософт",
    "google": "гугл", "apple": "+эпл", "anthropic": "антр+опик",
    "claude": "клод", "wi": "вай", "fi": "фай", "com": "ком", "org": "орг",
    "net": "нет", "ru": "ру", "txt": "т+экст", "png": "п+энг", "mp": "эм п+э",
    "wav": "вав", "json": "джейс+он", "html": "эйч ти эм +эль",
    "ruaccent": "ру акц+ент", "notes": "н+оутс", "audiobook": "+аудиокн+ига",
    "requirements": "реквайрментс", "build": "билд", "app": "+апп",
    "assets": "+ассетс", "docs": "докс", "install": "инст+олл",
    "pip": "пип", "cpu": "си пи ю", "gpu": "джи пи ю", "ram": "р+ам",
    "with": "виз", "py": "пай", "md": "эм ди", "sha": "ш+а", "url": "ю ар эль",
    "http": "эйч ти ти пи", "https": "эйч ти ти пи +эс",
    # расширения и имена файлов, как их называют по-русски
    "txt": "т+экст", "acc": "+акк", "log": "лог", "ini": "+ини",
    "audio": "+аудио", "video": "в+идео", "folder": "ф+олдер",
    "edits": "+эдитс", "cache": "кэш", "json": "джейс+он",
    # как произносит сам автор; пишем слитно, иначе правило односложных
    # слов добавит ударение ещё и на «дэ», и нажима станет два
    "dbacchus": "деб+ахус",
}
# названия латинских букв, как их читают в аббревиатурах
LATIN_LETTERS = {"a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и",
                 "f": "эф", "g": "джи", "h": "эйч", "i": "ай", "j": "джей",
                 "k": "кей", "l": "эль", "m": "эм", "n": "эн", "o": "оу",
                 "p": "пи", "q": "кью", "r": "ар", "s": "эс", "t": "ти",
                 "u": "ю", "v": "ви", "w": "д+аблъю", "x": "экс", "y": "уай",
                 "z": "зед"}
# буквосочетания читаются раньше отдельных букв
LATIN_DIGRAPHS = [
    ("sch", "ш"), ("tch", "ч"), ("igh", "ай"), ("tion", "шн"),
    ("ch", "ч"), ("sh", "ш"), ("th", "т"), ("ph", "ф"), ("wh", "в"),
    ("ck", "к"), ("qu", "кв"), ("ng", "нг"), ("ee", "и"), ("oo", "у"),
    ("ou", "ау"), ("ow", "ау"), ("ea", "и"), ("ai", "эй"), ("ay", "эй"),
    ("ey", "эй"), ("oa", "оу"), ("oi", "ой"), ("oy", "ой"), ("au", "о"),
    ("aw", "о"), ("ew", "ю"), ("ie", "и"),
]
LAT2CYR = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
           "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
           "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "а",
           "v": "в", "w": "у", "x": "кс", "y": "и", "z": "з"}


def _roman_value(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s.upper()):
        v = _ROMAN_VAL.get(ch, 0)
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def _gender_of(word: str) -> str:
    w = (word or "").lower().strip()
    if w in GENDER:
        return GENDER[w]
    if w.endswith(("а", "я")):
        return "f"
    if w.endswith(("о", "е")):
        return "n"
    return "m"


def expand_roman(text: str) -> str:
    """«Интерлюдия I» -> «Интерлюдия первая» (род по предыдущему слову).

    Однобуквенные цифры (I, V, X, C) разворачиваем только после слов вроде
    «глава» или «часть»: иначе английское «I will be back» превращается
    в «первая will be back», а «C» — в «сотый»."""
    def repl(m):
        s = m.group(0)
        if not s:
            return s
        n = _roman_value(s)
        if not 1 <= n <= len(ORDINALS["m"]):
            return s
        before = text[:m.start()].rstrip()
        prev = (re.findall(r"[А-Яа-яЁё]+", before) or [""])[-1]
        if len(s) == 1 and prev.lower() not in GENDER:
            return s
        word = ORDINALS[_gender_of(prev)][n - 1]
        return word.capitalize() if s.isupper() and not prev else word
    return ROMAN_RE.sub(repl, text)


LATIN_LONG = {"a": "эй", "e": "и", "i": "ай", "o": "оу", "u": "ю"}
_CONS = "bcdfghjklmnpqrstvwxz"

# Английские слова читаем по их настоящему произношению, а не по буквам: в
# CMUdict (Carnegie Mellon, лицензия BSD, файл data/cmudict.dict.gz) записаны
# фонемы и ударение для 126 тысяч слов. Отсюда «у+индоуз», а не «виндовс»,
# и ударение в нужном месте. Английскую модель Silero взять нельзя: она под
# CC BY-NC, да и голос посреди русской фразы менялся бы на чужой.
ARPA_VOWELS = {
    "AA": ("а", "о"), "AE": ("э", "э"), "AH": ("а", "а"), "AO": ("о", "о"),
    "AW": ("ау", "ау"), "AY": ("ай", "ай"), "EH": ("э", "э"), "ER": ("эр", "эр"),
    "EY": ("эй", "эй"), "IH": ("и", "и"), "IY": ("и", "и"), "OW": ("оу", "оу"),
    "OY": ("ой", "ой"), "UH": ("у", "у"), "UW": ("у", "у"),
}
ARPA_CONS = {
    "B": "б", "CH": "ч", "D": "д", "DH": "з", "F": "ф", "G": "г", "HH": "х",
    "JH": "дж", "K": "к", "L": "л", "M": "м", "N": "н", "NG": "нг", "P": "п",
    "R": "р", "S": "с", "SH": "ш", "T": "т", "TH": "т", "V": "в", "W": "у",
    "Y": "й", "Z": "з", "ZH": "ж",
}
_CMUDICT = None


def load_cmudict() -> dict:
    """Словарь произношений: слово -> фонемы. Читается один раз и лениво."""
    global _CMUDICT
    if _CMUDICT is not None:
        return _CMUDICT
    _CMUDICT = {}
    for place in (os.path.join(app_root(), "data", "cmudict.dict.gz"),
                  os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "cmudict.dict.gz")):
        if not os.path.exists(place):
            continue
        try:
            import gzip
            with gzip.open(place, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line or line[0] in "#;":
                        continue
                    word, _, rest = line.partition(" ")
                    if not rest:
                        continue
                    word = word.split("(")[0]          # «read(2)» -- второй вариант
                    if word not in _CMUDICT:
                        _CMUDICT[word] = rest.split()
            break
        except Exception:
            pass
    return _CMUDICT


def arpabet_to_russian(phones) -> str:
    """Фонемы -> русская запись со знаком ударения."""
    out = []
    bases = [p[:-1] if p[-1:].isdigit() else p for p in phones]
    for i, ph in enumerate(phones):
        stress = ph[-1] if ph[-1:].isdigit() else ""
        base = bases[i]
        nxt = bases[i + 1] if i + 1 < len(bases) else ""
        prev = bases[i - 1] if i else ""
        if base in ARPA_VOWELS:
            strong, weak = ARPA_VOWELS[base]
            sound = strong if stress == "1" else weak
            out.append(("+" if stress == "1" else "") + sound)
        elif base == "NG" and nxt in ("G", "K"):
            out.append("н")               # «language»: не «лэнггуадж»
        elif base == "Y" and prev in ARPA_CONS and nxt in ARPA_VOWELS:
            out.append("ь")               # «beautiful»: «бьютифул», не «бйутифул»
        else:
            out.append(ARPA_CONS.get(base, ""))
    return "".join(out)


def _read_latin_word(w: str) -> str:
    """Одно латинское слово по-русски."""
    low = w.lower()
    if low in LATIN_WORDS:
        return LATIN_WORDS[low]
    # аббревиатура: всё заглавными или нет ни одной гласной («MIT», «GPL»)
    if len(w) <= 5 and (w.isupper() or not set(low) & set("aeiouy")):
        return " ".join(LATIN_LETTERS.get(c, c) for c in low)
    phones = load_cmudict().get(low)
    if phones:
        said = arpabet_to_russian(phones)
        if said:
            return said
    # немая «e» на конце: «example» -> «ексампл», а в коротком слове она ещё и
    # удлиняет предыдущую гласную («base» -> «бейс», «note» -> «ноут»)
    if (len(low) > 3 and low.endswith("e") and low[-2] in _CONS
            and sum(c in "aeiouy" for c in low) >= 2):
        stem = low[:-1]
        m = re.search(r"([aeiou])([" + _CONS + r"])$", stem)
        if m and len(stem) <= 5:
            stem = stem[:m.start(1)] + LATIN_LONG[m.group(1)] + m.group(2)
        low = stem
    out = low.replace("ui", "и")
    out = re.sub(r"c(?=[eiy])", "s", out)        # «cis» -> «сис», «code» -> «коуд»
    for pair, rep in LATIN_DIGRAPHS:
        out = out.replace(pair, rep)
    return "".join(LAT2CYR.get(c, c) for c in out)


def latin_to_cyrillic(text: str) -> str:
    """Латиница русскими буквами: по словарю, по названиям букв или по
    правилам чтения. Точки внутри адресов произносим словом, иначе
    «github.com» слипается в одно непроизносимое слово."""
    def repl(m):
        word = m.group(0)
        parts = word.split(".")
        said = [_read_latin_word(p) if p else "" for p in parts]
        return " т+очка ".join(s for s in said if s)
    return re.sub(r"[A-Za-z][A-Za-z.]*[A-Za-z]|[A-Za-z]", repl, text)


def normalize_latin(text: str):
    """Текст без латиницы + список того, что заменили (для журнала)."""
    found = re.findall(r"[A-Za-z]+", text)
    if not found:
        return text, []
    out = expand_roman(text)
    # «v5_cis_base» -- это не слово, а три куска и число: подчёркивания и
    # стыки букв с цифрами разводим пробелами, иначе всё слипается в
    # «ви5кисбэйс», да и число потом не развернётся в слово
    out = re.sub(r"(?<=[A-Za-z0-9])[_/](?=[A-Za-z0-9])", " ", out)
    out = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", out)
    out = re.sub(r"(?<=\d)\.(?=\d)", " т+очка ", out)     # «3.10» -> «три точка десять»
    # точка перед расширением: «Аудиокнига.exe» иначе звучит как конец
    # предложения, а «(.txt или .md)» слипается в одно слово
    out = re.sub(r"(?<=[А-Яа-яЁё0-9])\.(?=[A-Za-z])", " т+очка ", out)
    out = re.sub(r"(?<=[\s(«\"'])\.(?=[A-Za-z])", "т+очка ", out)
    return latin_to_cyrillic(out), found


# ---------------------------------------------------------------- числа
# Цифр в алфавите Silero тоже нет, и она их молча выбрасывает: «15 лет»
# читается как «лет» (услышано 2026-09-17). Разворачиваем числа в слова на
# этапе расстановки ударений -- в файле с ударениями видно, что получилось,
# и можно поправить руками.

_ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь",
         "девять", "десять", "одиннадцать", "двенадцать", "тринадцать",
         "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
         "восемнадцать", "девятнадцать"]
_ONES_F = {1: "одна", 2: "две"}
# родительный падеж количественных: «около сорока пяти лет»
_GEN_ONES = ["", "одного", "двух", "трёх", "четырёх", "пяти", "шести", "семи",
             "восьми", "девяти", "десяти", "одиннадцати", "двенадцати",
             "тринадцати", "четырнадцати", "пятнадцати", "шестнадцати",
             "семнадцати", "восемнадцати", "девятнадцати"]
_GEN_TENS = ["", "", "двадцати", "тридцати", "сорока", "пятидесяти",
             "шестидесяти", "семидесяти", "восьмидесяти", "девяноста"]
_GEN_HUNDREDS = ["", "ста", "двухсот", "трёхсот", "четырёхсот", "пятисот",
                 "шестисот", "семисот", "восьмисот", "девятисот"]
GEN_PREPS = ("около", "до", "от", "свыше", "более", "менее", "порядка", "за")
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят",
         "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот",
             "семьсот", "восемьсот", "девятьсот"]
_ORD_ONES = {1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый",
             6: "шестой", 7: "седьмой", 8: "восьмой", 9: "девятый", 10: "десятый",
             11: "одиннадцатый", 12: "двенадцатый", 13: "тринадцатый",
             14: "четырнадцатый", 15: "пятнадцатый", 16: "шестнадцатый",
             17: "семнадцатый", 18: "восемнадцатый", 19: "девятнадцатый"}
_ORD_TENS = {20: "двадцатый", 30: "тридцатый", 40: "сороковой", 50: "пятидесятый",
             60: "шестидесятый", 70: "семидесятый", 80: "восьмидесятый",
             90: "девяностый"}
_ORD_HUNDREDS = {100: "сотый", 200: "двухсотый", 300: "трёхсотый",
                 400: "четырёхсотый", 500: "пятисотый", 600: "шестисотый",
                 700: "семисотый", 800: "восьмисотый", 900: "девятисотый"}
_ORD_THOUSANDS = {1000: "тысячный", 2000: "двухтысячный", 3000: "трёхтысячный"}
MONTHS = ("января февраля марта апреля мая июня июля августа сентября октября "
          "ноября декабря").split()
# падеж порядкового по слову рядом: «в 2038 году» -> «в две тысячи тридцать восьмом»
YEAR_CASE = {"год": "nom", "года": "gen", "году": "pre", "годом": "ins",
             "годе": "pre", "г": "nom"}
SUFFIX_CASE = {"й": "nom", "го": "gen", "му": "dat", "м": "ins", "ом": "pre"}
COUNT_NOUNS = ("лет год года года́ дней день дня раз раза часов часа минут "
               "секунд рублей человек шагов вёрст километров метров").split()


def _cardinal_below_1000(n: int, feminine: bool = False) -> list:
    out = []
    if n >= 100:
        out.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10])
        n %= 10
    if n:
        out.append(_ONES_F[n] if (feminine and n in _ONES_F) else _ONES[n])
    return out


def cardinal(n: int, feminine: bool = False) -> str:
    """Количественное числительное: 2038 -> «две тысячи тридцать восемь»."""
    if n == 0:
        return "ноль"
    words = []
    if n >= 1000:
        th = n // 1000
        words += _cardinal_below_1000(th, feminine=True)
        last = th % 100
        if 11 <= last <= 14:
            words.append("тысяч")
        elif last % 10 == 1:
            words.append("тысяча")
        elif last % 10 in (2, 3, 4):
            words.append("тысячи")
        else:
            words.append("тысяч")
        n %= 1000
    words += _cardinal_below_1000(n, feminine)
    return " ".join(w for w in words if w)


def cardinal_gen(n: int) -> str:
    """Количественное в родительном: «сорока пяти» (для «около 45 лет»)."""
    if n >= 1000 or n <= 0:
        return cardinal(n)
    out = []
    h, r = divmod(n, 100)
    if h:
        out.append(_GEN_HUNDREDS[h])
    if r >= 20:
        t, o = divmod(r, 10)
        out.append(_GEN_TENS[t])
        if o:
            out.append(_GEN_ONES[o])
    elif r:
        out.append(_GEN_ONES[r])
    return " ".join(out)


def _thousand_form(th: int) -> str:
    last = th % 100
    if 11 <= last <= 14:
        return "тысяч"
    return {1: "тысяча", 2: "тысячи", 3: "тысячи", 4: "тысячи"}.get(last % 10, "тысяч")


def ordinal(n: int) -> str:
    """Порядковое: 2038 -> «две тысячи тридцать восьмой». Порядковым
    становится только последняя значащая часть числа."""
    if n in _ORD_THOUSANDS:
        return _ORD_THOUSANDS[n]
    head = []
    th, rest = divmod(n, 1000)
    if th:
        head += _cardinal_below_1000(th, feminine=True)
        head.append(_thousand_form(th))
    if rest == 0:                      # 5000 и подобные в книгах не встречаются
        return " ".join(head)
    h, r = divmod(rest, 100)
    if r == 0:
        return " ".join(head + [_ORD_HUNDREDS[h * 100]])
    if h:
        head.append(_HUNDREDS[h])
    if r < 20:
        return " ".join(head + [_ORD_ONES[r]])
    t, o = divmod(r, 10)
    if o == 0:
        return " ".join(head + [_ORD_TENS[t * 10]])
    head.append(_TENS[t])
    return " ".join(head + [_ORD_ONES[o]])


def decline_ordinal(word: str, case: str) -> str:
    """Склонение последнего слова порядкового числительного."""
    if case in (None, "nom"):
        return word
    if word.endswith("ий"):                      # третий
        stem, forms = word[:-2], {"gen": "ьего", "dat": "ьему", "ins": "ьим",
                                  "pre": "ьем", "acc": "ий"}
    elif word.endswith(("ый", "ой")):
        stem, forms = word[:-2], {"gen": "ого", "dat": "ому", "ins": "ым",
                                  "pre": "ом", "acc": word[-2:]}
    else:
        return word
    return stem + forms.get(case, word[-2:])


def year_words(n: int, case: str = "nom") -> str:
    parts = ordinal(n).split()
    parts[-1] = decline_ordinal(parts[-1], case)
    return " ".join(parts)


def expand_numbers(text: str) -> str:
    """Числа -> слова, с оглядкой на соседей: год, дата, возраст, номер главы."""
    months = "|".join(MONTHS)

    # 1. «2 июля 2036» -> «второго июля две тысячи тридцать шестого»
    def date_repl(m):
        day, month, year = int(m.group(1)), m.group(2), m.group(3)
        out = decline_ordinal(ordinal(day), "gen") + " " + month
        if year:
            out += " " + year_words(int(year), "gen")
        return out
    text = re.sub(rf"\b(\d{{1,2}})\s+({months})(?:\s+(\d{{4}}))?", date_repl, text)

    # 2. диапазон годов «2028–2031»
    text = re.sub(r"\b(1[89]\d\d|20\d\d)\s*[-–—]\s*(1[89]\d\d|20\d\d)\b",
                  lambda m: f"{year_words(int(m.group(1)))} — {year_words(int(m.group(2)))}",
                  text)

    # 3. год с наращением: «2046-му», «2041-го»
    text = re.sub(r"\b(1[89]\d\d|20\d\d)-(й|го|му|м|ом)\b",
                  lambda m: year_words(int(m.group(1)),
                                       SUFFIX_CASE.get(m.group(2), "nom")), text)

    # 4. год рядом со словом «год/года/году»
    def year_noun(m):
        n, word = int(m.group(1)), m.group(2)
        return f"{year_words(n, YEAR_CASE.get(word.lower(), 'nom'))} {word}"
    text = re.sub(r"\b(1[89]\d\d|20\d\d)\s+(год\w*)\b", year_noun, text)

    # 5. «Глава 1», «Часть 2» -> порядковое в роде слова
    def chapter_num(m):
        word, n = m.group(1), int(m.group(2))
        if not 1 <= n <= len(ORDINALS["m"]):
            return m.group(0)
        return f"{word} {ORDINALS[_gender_of(word)][n - 1]}"
    text = re.sub(r"\b([А-ЯЁ][а-яё]+)\s+(\d{1,2})(?=[.\s,:])",
                  lambda m: chapter_num(m) if m.group(1).lower() in GENDER else m.group(0),
                  text)

    # 6. одинокий год: «2037, Тёмный год» -- в книге четырёхзначные это годы
    text = re.sub(r"\b(1[89]\d\d|20\d\d)\b", lambda m: year_words(int(m.group(1))), text)

    # 7. всё остальное -- количественное; после «около», «до», «свыше» и
    #    подобных нужен родительный: «около сорока пяти лет»
    def plain_num(m):
        before, after = m.group(1) or "", m.group(3) or ""
        n = int(m.group(2))
        if before.strip().lower() in GEN_PREPS:
            words = cardinal_gen(n)
        else:
            fem = after.strip().lower().startswith(
                ("тысяч", "минут", "секунд", "недел", "верст", "вёрст", "сажен"))
            words = cardinal(n, fem)
        return before + words + after
    text = re.sub(r"([А-Яа-яЁё]+ )?\b(\d+)(\s+[А-Яа-яЁё]+)?", plain_num, text)
    return text


# Сокращения, которые модель проговаривает невнятно: две заглавные буквы она
# читает как слог. Разворачиваем в слова -- «73 МБ» должно звучать полностью.
ABBR_RU = {
    "МБ": "мегаб+айт", "ГБ": "гигаб+айт", "КБ": "килоб+айт", "ТБ": "тераб+айт",
    "Кб": "килоб+айт", "Мб": "мегаб+айт", "Гб": "гигаб+айт",
    "кг": "килогр+амм", "мм": "миллим+етр", "см": "сантим+етр",
    "км": "килом+етр", "мин": "мин+ут", "сек": "сек+унд",
}
# Стрелки и прочие значки: модель их выбрасывает, и фраза склеивается.
SIGNS = {"->": ",", "→": ",", "=>": ",", "<-": ",", "←": ",",
         "±": "пл+юс-м+инус", "×": "на", "≈": "пр+иблизительно",
         "№": "н+омер", "%": "проц+ентов", "&": "и", "@": "соб+ака"}


def expand_signs(text: str) -> str:
    """Значки и сокращения -- словами, иначе они просто пропадают."""
    text = text.replace("--", " — ")                  # двойной дефис -- это тире
    text = re.sub(r"(?<=\d)\.(?=\d)", " т+очка ", text)   # «1.0» -> «один точка ноль»
    for sign, word in SIGNS.items():
        if sign in text:
            text = text.replace(sign, f" {word} " if word.isalpha() else word + " ")
    def abbr(m):
        return ABBR_RU.get(m.group(0), m.group(0))
    return re.sub(r"(?<![А-Яа-яЁё])(" + "|".join(ABBR_RU) + r")(?![А-Яа-яЁё])",
                  abbr, text)


def mark_first_vowel(plain: str) -> str:
    for i, ch in enumerate(plain):
        if ch.lower() in VOWELS:
            return plain[:i] + "+" + plain[i:]
    return plain


def mark_yo(plain: str) -> str:
    """«+» перед «ё».

    «Ё» в русском всегда ударная, и Silero это знает -- но только в своём
    режиме расстановки (put_accent=True): в коде модели «if user has set only
    'ё' letters, we will only assign stress to those letters». Мы размечаем
    текст сами и её расстановку выключаем, поэтому слово с «ё» без знака
    уходит в синтез безударным и читается через «е» («извёстка» -> «известка»,
    услышано 2026-09-17). В сложных словах ударная обычно вторая часть, так
    что знак ставим перед ПОСЛЕДНЕЙ «ё» («трёхколёсный» -> «трёхкол+ёсный»).
    """
    i = plain.lower().rfind("ё")
    return plain[:i] + "+" + plain[i:] if i >= 0 else plain


def count_vowels(word: str) -> int:
    return sum(ch in VOWELS for ch in word.lower())


def stress_variants(word: str):
    """Все способы прочитать слово: [(форма, подпись), ...].

    Это и позиции ударения ("+" перед гласной), и замены «е» на «ё» -- в
    таких словах, как «все/всё» или «берет/берёт», выбор буквы и есть выбор
    смысла. Знак ставится и перед «ё»: она всегда ударная, но Silero с
    выключенной своей расстановкой этого не знает (см. mark_yo).
    """
    plain = word.replace("+", "")
    out = []
    for i, ch in enumerate(plain):
        if ch.lower() in VOWELS:
            out.append((plain[:i] + "+" + plain[i:], "ударение"))
    for i, ch in enumerate(plain):
        if ch.lower() == "е":
            yo = "ё" if ch.islower() else "Ё"
            form = plain[:i] + "+" + yo + plain[i + 1:]     # знак перед ё обязателен
            out.append((form, "через ё"))
    for i, ch in enumerate(plain):
        if ch.lower() == "ё":                      # обратная замена: ё -> е
            e = "е" if ch.islower() else "Е"
            base = plain[:i] + e + plain[i + 1:]
            for j, c2 in enumerate(base):
                if c2.lower() in VOWELS:
                    out.append((base[:j] + "+" + base[j:], "через е"))
    seen, uniq = set(), []
    for form, note in out:
        if form not in seen:
            seen.add(form)
            uniq.append((form, note))
    return uniq


class Accentizer:
    """RUAccent + пользовательский словарь + правила односложных слов."""

    def __init__(self, dictionary=None, cache_path=None, model_size="turbo3", log=print):
        self.log = log
        self.model_size = model_size
        self.acc = None
        self.author_yo = False        # текст ёфицирован -> «е» автора не трогаем
        self.cache_path = cache_path
        self.cache = {}
        self._word_stress = {}
        self._dict = {}
        self._re = None
        self.set_dictionary(dictionary or {})
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as f:
                    self.cache = json.load(f)
            except Exception:
                self.cache = {}

    # -- словарь -------------------------------------------------------
    def set_dictionary(self, d: dict):
        self._dict = {k.lower(): v for k, v in d.items() if k and v}
        if self._dict:
            keys = sorted(self._dict, key=len, reverse=True)
            self._re = re.compile(r"\b(" + "|".join(map(re.escape, keys)) + r")\b",
                                  re.IGNORECASE)
        else:
            self._re = None

    def _dict_form(self, word: str) -> str:
        """Словарная форма с сохранением заглавной буквы исходного слова."""
        key = word.lower()
        v = self._dict.get(key) or self._dict.get(key.replace("ё", "е"))
        if v is None:
            return word
        if word[:1].isupper():
            v = ("+" + v[1].upper() + v[2:]) if v.startswith("+") else (v[0].upper() + v[1:])
        return v

    def apply_dictionary(self, text: str) -> str:
        """Словарь поверх вывода RUAccent: снимаем "+", ищем слово, подставляем.
        RUAccent переставляет знак в словах из СВОЕГО словаря заново
        (к+арбас -> карб+ас), поэтому наш словарь применяется последним."""
        if not self._dict:
            return text

        def repl(m):
            w = m.group(0)
            plain = w.replace("+", "")
            if not plain:
                return w
            key = plain.lower()
            if key not in self._dict and key.replace("ё", "е") not in self._dict:
                return w
            return self._dict_form(plain)

        return WORD_RE.sub(repl, text)

    # -- RUAccent ------------------------------------------------------
    def load(self):
        """Тяжёлая загрузка моделей. Возвращает True, если RUAccent доступен."""
        if self.acc is not None:
            return True
        try:
            from ruaccent import RUAccent
            patch_pycrfsuite_paths()      # путь с кириллицей -> короткое имя
            acc = RUAccent()
            # В собранной версии модели лежат рядом с exe: workdir принимает
            # load(), а не конструктор, так что прятать их внутрь не нужно.
            kw = dict(omograph_model_size=self.model_size, use_dictionary=True,
                      tiny_mode=False)
            workdir = ruaccent_workdir()
            if workdir:
                kw["workdir"] = workdir
            acc.load(**kw)
            self._fix_token_type_ids(acc)
            self.acc = acc
            self.log(f"RUAccent загружен (модель омографов: {self.model_size})")
            return True
        except Exception as e:
            self.log(f"RUAccent недоступен: {e}")
            return False

    @staticmethod
    def _fix_token_type_ids(acc):
        """transformers >= 5 не возвращает token_type_ids, а ONNX-модели RUAccent
        их требуют (падает на первом же слове вне словаря). Оборачиваем токенизатор
        подмоделей, которым они нужны, — правка живёт здесь, а не в site-packages."""
        import numpy as np

        def wrap(tok):
            def call(*a, **k):
                out = tok(*a, **k)
                if "token_type_ids" not in out:
                    out["token_type_ids"] = np.zeros_like(out["input_ids"])
                return out
            return call

        for name in ("accent_model", "omograph_model", "stress_usage_model",
                     "yo_homograph_model"):
            m = getattr(acc, name, None)
            tok = getattr(m, "tokenizer", None)
            sess = getattr(m, "session", None)
            if tok is None or sess is None:
                continue
            try:
                wants = {i.name for i in sess.get_inputs()}
            except Exception:
                continue
            if "token_type_ids" in wants:
                m.tokenizer = wrap(tok)

    # -- авторская «ё» ---------------------------------------------------
    def set_source_policy(self, text: str) -> bool:
        """Ёфицирован ли текст, то есть пишет ли автор «ё» осознанно.

        Если пишет, то его «е» -- тоже выбор, и менять его нельзя: RUAccent
        иначе читает «не швартовались лет пятнадцать» как «л+ёт», «молчит
        перед тем» как «пер+ёд», «Все за» как «Вс+ё за» (34 таких подмены на
        повести «Порог», 2026-09-17). В неёфицированном тексте, наоборот,
        восстанавливать «ё» нужно, и решение остаётся за моделью."""
        words = WORD_RE.findall(text)
        n_yo = sum(1 for w in words if "ё" in w.lower())
        self.author_yo = bool(words) and n_yo / len(words) >= 0.003
        return self.author_yo

    def _keep_author_yo(self, src: str, out: str) -> str:
        """Вернуть авторское «е» там, где модель поставила «ё» ПО ВЫБОРУ.

        Различаем два словаря RUAccent: `yo_words` (85 568 слов) -- однозначная
        ёфикация, слова без «ё» просто не существует («пошел» -> «пошёл»), её
        оставляем всегда; `yo_homographs` (637 слов) -- оба написания
        существуют, и выбор делает модель, вот его и откатываем. Ударение для
        «е»-формы берём из словаря `accents`: сам RUAccent даже на отдельном
        слове отвечает ё-вариантом («перед» -> «пер+ёд»)."""
        if not self.author_yo or self.acc is None:
            return out
        homo = getattr(self.acc, "yo_homographs", None) or {}
        table = getattr(self.acc, "accents", None) or {}
        if not homo or not table:
            return out
        src_words = WORD_RE.findall(src)
        if len(src_words) != len(WORD_RE.findall(out)):
            return out
        pos = [0]

        def repl(m):
            y = m.group(0)
            i = pos[0]
            pos[0] += 1
            x = src_words[i]
            plain = y.replace("+", "")
            if "ё" in x.lower() or "ё" not in plain.lower():
                return y
            if plain.lower().replace("ё", "е") != x.lower():
                return y                       # слово изменилось не только «ё»
            key = x.lower()
            if key not in homo:
                return y                       # ёфикация однозначная
            form = table.get(key)
            if not form or "ё" in form:
                return y
            if x[:1].isupper():
                form = ("+" + form[1].upper() + form[2:]) if form.startswith("+") \
                       else form[0].upper() + form[1:]
            return form

        return WORD_RE.sub(repl, out)

    def yo_choice_words(self) -> set:
        """Слова, в которых выбор «е»/«ё» делает модель (для проверки глазами)."""
        homo = getattr(self.acc, "yo_homographs", None) or {}
        out = set()
        for k, v in homo.items():
            out.add(k)
            out.add(v.replace("ё", "е"))
        return out

    def _raw(self, text: str) -> str:
        """RUAccent с кэшем: его результат для куска текста не меняется, а сам он
        занимает ~80% времени прогона."""
        if self.acc is None:
            return text
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key not in self.cache:
            self.cache[key] = self.acc.process_all(text)
        return self.cache[key]

    def save_cache(self):
        if self.cache_path:
            try:
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump(self.cache, f, ensure_ascii=False)
            except Exception as e:
                self.log(f"Кэш не сохранён: {e}")

    def _standalone_stress(self, plain: str) -> str:
        """Ударение многосложного слова, которое RUAccent в контексте оставил без
        знака (его модель снимает "+" с «это», «было», «если», «между»...).
        При put_accent=False Silero читает такое слово с редуцированной гласной."""
        low = plain.lower()
        if low not in self._word_stress:
            s = self.acc.process_all(low) if self.acc is not None else low
            s = re.sub(r"[^А-Яа-яЁё+]", "", s)
            if "ё" in s and "ё" not in low:
                # Спрашивая слово в отрыве от фразы, мы спрашиваем, КУДА
                # ударение, а не КАК его писать: на отдельном «перед» RUAccent
                # отвечает «пер+ёд», хотя в самой фразе он эту «ё» не ставил.
                # Букву оставляем авторскую, ударение берём из его словаря.
                table = getattr(self.acc, "accents", None) or {}
                alt = table.get(low)
                s = alt if (alt and "ё" not in alt) else s.replace("ё", "е")
            self._word_stress[low] = s if "+" in s else mark_first_vowel(low)
        s = self._word_stress[low]
        if plain[:1].isupper():
            s = ("+" + s[1].upper() + s[2:]) if s.startswith("+") else (s[0].upper() + s[1:])
        return s

    def _lower_initial_homographs(self, text: str) -> tuple[str, set]:
        """Омограф в начале предложения опустить в строчные (для модели).

        RUAccent принимает слово с большой буквы за имя собственное и
        омограф в нём не разбирает: «Потом были худые годы» -> «П+отом»
        (через пот), хотя «потом были...» -> «пот+ом». В повести так
        испорчены все 27 «Потом» в начале фраз, а те же 57 в середине
        разобраны верно (найдено на слух 2026-09-20).

        Трогаем только слова из ЕГО же словаря омографов (19741 слово):
        «Аня», «Кузьма», «Тихон» туда не входят и остаются как есть.
        Возвращает текст и множество снятых слов."""
        omo = getattr(self.acc, "omographs", None) or {}
        if not omo:
            return text, set()
        lowered = set()

        def sub(m):
            w = m.group(3)
            if not w[:1].isupper() or w.lower() not in omo:
                return m.group(0)
            lowered.add(w.lower())
            return m.group(1) + m.group(2) + w[0].lower() + w[1:]

        return SENTENCE_FIRST_WORD.sub(sub, text), lowered

    def _restore_initial_caps(self, marked: str, lowered: set) -> str:
        """Вернуть заглавную букву там, где мы её сняли.

        Ищем по САМИМ словам, а не по порядковым номерам предложений:
        нумерация разъезжается, если в исходнике есть markdown. В абзаце
        «**Аня Корнева**, 15 лет. Внучка. Ноги как у цапли» звёздочки не дают
        шаблону совпасть в начале, а в размеченном тексте их уже нет -- и
        «Ноги» теряли заглавную. Слово в начале предложения и так должно быть
        с большой, так что лишним это исправление быть не может."""
        if not lowered:
            return marked

        def sub(m):
            w = m.group(3)
            # знак ударения может стоять перед первой буквой: «+это»
            i = 1 if w.startswith("+") else 0
            if w.replace("+", "").lower() not in lowered:
                return m.group(0)
            return m.group(1) + m.group(2) + w[:i] + w[i].upper() + w[i + 1:]

        return SENTENCE_FIRST_WORD.sub(sub, marked)

    def fix_monosyllables(self, text: str) -> str:
        out = []
        for sent in re.split(r"(?<=[.!?…])\s+", text):

            def fix(m):
                w = m.group(0)
                plain = w.replace("+", "")
                low = plain.lower()
                nv = count_vowels(low)
                if nv == 0:
                    return w
                if nv >= 2:
                    if "+" in w:
                        return w
                    if "ё" in low:
                        return mark_yo(plain)      # ё ударная, но знак нужен
                    return self._standalone_stress(plain)
                if low in FUNCTION_WORDS:
                    return plain                      # служебное: без ударения
                if low == "то" and POSTFIX_TO.search(sent[:m.start()]):
                    return plain      # постфикс «-то»: «что-то», «где-то»
                if low == "что" and sent[:m.start()].rstrip().endswith(":"):
                    # «бояться надо было другого: что они научатся думать» --
                    # после двоеточия «что» вводит пояснение и звучит слитно с
                    # ним; с ударением получается лишний нажим (на слух
                    # 2026-09-17). После запятой, наоборот, ударение нужно.
                    return plain
                return w if "+" in w else mark_first_vowel(plain)

            out.append(NEG_RETRACT.sub(fix_neg_retract,
                           CONCESSIVE.sub(fix_concessive, WORD_RE.sub(fix, sent))))
        return " ".join(out)

    def process(self, text: str) -> str:
        """Полная цепочка для одного куска текста."""
        text = expand_signs(text)
        text, latin = normalize_latin(text)
        if latin:
            self.log("Латиница заменена: " + ", ".join(dict.fromkeys(latin))[:120])
        if any(ch.isdigit() for ch in text):
            text = expand_numbers(text)
        if self.acc is None:
            return self.apply_dictionary(text)
        lowered, caps = self._lower_initial_homographs(text)
        raw = self._restore_initial_caps(self._raw(lowered), caps)
        raw = self._keep_author_yo(text, raw)
        # RUAccent считает «(» пунктуацией и убирает пробел перед ней
        # («Перемена (хронология)» -> «Перемена(хронология)»), а Silero скобку
        # выбрасывает -- слова склеились бы в одно. Возвращаем пробел.
        raw = re.sub(r"(?<=[А-Яа-яЁё0-9])([(\[«])", r" \1", raw)
        return self.fix_monosyllables(self.apply_dictionary(raw))


def accent_file_text(text: str, acc: Accentizer, progress=None, cancel=None,
                     edits: dict | None = None, workers: int = 1) -> tuple[str, int]:
    """Проставить ударения во всём файле, сохранив его строчную структуру
    (заголовки "# ", разделители "***", пустые строки -- строка в строку, это
    нужно для привязки ручных правок).

    edits: {sha1(строка исходника): {"fixed": строка человека, "base":
    автоматическая строка на тот момент}}. Строка человека НЕ подменяет
    результат целиком: автомат размечает её заново, а затем поверх ложатся
    только те слова, которые человек действительно менял (merge_edit) --
    иначе правка замораживает строку и поздние правила до неё не доходят.
    Старый формат (просто строка) накладывается целиком, как раньше.
    Возвращает (текст, сколько правок легло)."""
    edits = edits or {}
    if acc is not None and acc.acc is not None:
        if acc.set_source_policy(text):
            acc.log("Текст ёфицирован: буква «е» автора сохраняется "
                    "(однозначная «ё» всё равно восстанавливается)")
    lines = text.splitlines()
    out = list(lines)
    kept = 0
    jobs = []          # (номер строки, текст для модели, отступ заголовка)
    by_hand = {}       # строки с ручной правкой: {номер: (строка, база)}

    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or s in BREAK_MARKERS:
            continue
        hand = edits.get(line_key(s))
        if hand is not None:
            fixed, base = edit_parts(hand)
            kept += 1
            if base is None:
                out[i] = fixed             # старый формат: снимок целиком
                continue
            by_hand[i] = (fixed, base)     # свежую разметку получит ниже
        if s.startswith("#"):
            head = s.lstrip("#")
            jobs.append((i, head.strip(), s[:len(s) - len(head)] + " "))
        else:
            jobs.append((i, s, ""))

    total = max(1, len(jobs))
    done = 0

    def work(job):
        i, body, pad = job
        value = pad + acc.process(body)
        if i in by_hand:
            fixed, base = by_hand[i]
            value = merge_edit(value, fixed, base)
        return i, value

    # Абзацы независимы, а RUAccent на них потокобезопасен (проверено: тот же
    # результат, ускорение 3.9x на восьми потоках) -- первая расстановка по
    # книге занимает минуты, и это единственное место, где стоит распараллелить.
    if workers > 1 and len(jobs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(work, j) for j in jobs]
            for fut in futures:
                if cancel is not None and cancel.is_set():
                    for f in futures:
                        f.cancel()
                    break
                i, value = fut.result()
                out[i] = value
                done += 1
                if progress:
                    progress(done, total)
    else:
        for job in jobs:
            if cancel is not None and cancel.is_set():
                break
            i, value = work(job)
            out[i] = value
            done += 1
            if progress:
                progress(done, total)
    return "\n".join(out) + "\n", kept


def classify_word(word: str) -> str | None:
    """Чем это слово может быть интересно при проверке:
        'yo'        -- выбор е/ё решает смысл (все/всё, берет/берёт);
        'homograph' -- ударение решает смысл (замок/замок);
        'nostress'  -- многосложное слово осталось без ударения;
        None        -- проверять нечего.
    """
    plain = word.replace("+", "").lower()
    if not plain:
        return None
    flat = plain.replace("ё", "е")
    if flat in YO_AMBIGUOUS:
        return "yo"
    if plain in HOMOGRAPHS or flat in HOMOGRAPHS:
        return "homograph"
    if "+" not in word and "ё" not in plain and count_vowels(plain) >= 2:
        return "nostress"
    return None


def analyze(text: str, kinds=None):
    """Места, которые стоит просмотреть глазами.
    Возвращает список словарей: line, col, word, kind, context."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in WORD_RE.finditer(line):
            kind = classify_word(m.group(0))
            if kind is None or (kinds and kind not in kinds):
                continue
            a = max(0, m.start() - 42)
            b = min(len(line), m.end() + 42)
            ctx = ("…" if a else "") + line[a:b].strip() + ("…" if b < len(line) else "")
            out.append(dict(line=n, col=m.start(), word=m.group(0), kind=kind,
                            context=ctx))
    return out


# ---------------------------------------------------------------- ручные правки
# Словарь лечит слово ВЕЗДЕ. Но ударение часто зависит от места: «поставил замок»
# и «старый замок» -- одно слово, разное чтение. Такую правку нельзя держать в
# словаре, её надо помнить по месту. Помним по строке исходного текста: ключ --
# sha1 строки ИСХОДНИКА, значение -- строка с ударениями, как её поправили руками.
# Правки переживают повторную расстановку ударений и правку словаря; теряются
# только если сам исходный абзац изменился (тогда его читают заново).


def edits_path(acc_path: str) -> str:
    return acc_path + ".edits.json"


def load_edits(acc_path: str) -> dict:
    p = edits_path(acc_path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_edits(acc_path: str, edits: dict):
    p = edits_path(acc_path)
    if not edits:
        if os.path.exists(p):
            os.remove(p)
        return
    with open(p, "w", encoding="utf-8") as f:
        json.dump(edits, f, ensure_ascii=False, indent=1)


def line_key(src_line: str) -> str:
    return hashlib.sha1(src_line.strip().encode("utf-8")).hexdigest()


def edit_parts(value) -> tuple[str, str | None]:
    """Правка: (строка человека, автоматическая строка на момент правки).

    Старый формат -- просто строка, без второй половины: такие правки
    накладываются целиком, как раньше."""
    if isinstance(value, dict):
        return value.get("fixed", ""), value.get("base")
    return value, None


def merge_edit(fresh: str, fixed: str, base: str | None) -> str:
    """Наложить ручную правку на СВЕЖУЮ разметку, а не подменять её снимком.

    Правка хранится снимком всей строки, и это ловушка: строка замирает на
    том дне, когда её правили, и поздние правила до неё не доходят. Так
    правка «к+озлы» заодно заморозила «П+отом» -- омограф в начале
    предложения чинился позже (на слух 2026-09-20, Денис услышал «по́том»
    в уже исправленной книге).

    Зная автоматическую версию на момент правки, можно разобрать снимок
    пословно: где человек изменил слово -- берём его, остальное берём из
    свежей разметки. Если базы нет (правка старого формата) или строки
    разошлись по числу слов, накладываем снимок целиком, как раньше."""
    if base is None:
        return fixed
    f, b, x = fresh.split(" "), base.split(" "), fixed.split(" ")
    if not (len(f) == len(b) == len(x)):
        return fixed
    return " ".join(xw if xw != bw else fw for fw, bw, xw in zip(f, b, x))


CHECK_HEADER = """\
# Спорные места: {name}
#
# Здесь собраны только строки, в которых ударение или буква «ё» выбраны
# ПО СМЫСЛУ, то есть автомат мог ошибиться. Правьте прямо в строках.
#   «+» ставится ПЕРЕД ударной гласной:  зам+ок (на двери) / з+амок (крепость)
#   «ё» всегда ударная, и знак перед ней тоже нужен:  вс+е (они) / вс+ё (это)
# Номер в квадратных скобках не трогать, строк не добавлять и не удалять.
# Готовый файл загрузите кнопкой «Импорт проверенного».

"""


def export_check(acc_text: str, name: str = "", kinds=("yo", "homograph")) -> str:
    """Спорные строки в отдельный файл — просмотреть списком, а не перечитывая
    всю главу. Одна запись: номер строки, слова-поводы, сама строка."""
    found = analyze(acc_text, kinds)
    by_line = {}
    for it in found:
        by_line.setdefault(it["line"], []).append(it["word"])
    lines = acc_text.splitlines()
    out = [CHECK_HEADER.format(name=name or "глава")]
    for n in sorted(by_line):
        words = ", ".join(dict.fromkeys(by_line[n]))
        out.append(f"[{n:05d}] ({words})\n{lines[n - 1]}\n")
    out.append(f"# всего строк: {len(by_line)}\n")
    return "\n".join(out)


def parse_check(text: str) -> dict:
    """Обратный разбор файла проверки: {номер строки: исправленный текст}."""
    res, cur = {}, None
    for raw in text.splitlines():
        s = raw.rstrip()
        if not s.strip():
            continue
        m = re.match(r"^\[(\d+)\]", s)
        if m:
            cur = int(m.group(1))
            continue
        if s.lstrip().startswith("#"):
            cur = None
            continue
        if cur is not None:
            res[cur] = s
            cur = None
    return res


def apply_check(acc_text: str, fixes: dict) -> tuple[str, int]:
    """Подставить исправленные строки обратно в текст с ударениями."""
    lines = acc_text.splitlines()
    n = 0
    for ln, new in fixes.items():
        if 1 <= ln <= len(lines) and lines[ln - 1] != new:
            lines[ln - 1] = new
            n += 1
    return "\n".join(lines) + "\n", n


def collect_edits(src_text: str, old_acc: str, new_acc: str, edits: dict) -> tuple[dict, int]:
    """Строки, которые пользователь изменил руками (new_acc против old_acc),
    запоминаются под ключом соответствующей строки исходника.
    Требует построчного соответствия: расстановка ударений его сохраняет."""
    src = src_text.splitlines()
    old = old_acc.splitlines()
    new = new_acc.splitlines()
    if len(src) != len(new) or len(old) != len(new):
        return edits, -1               # строки добавлены/удалены: привязка потеряна
    n = 0
    for s, o, w in zip(src, old, new):
        if o != w and s.strip():
            # рядом со строкой человека храним автоматическую версию на этот
            # момент: по ней потом видно, какие именно слова он изменил, и
            # правка переживает появление новых правил (см. merge_edit)
            edits[line_key(s)] = {"fixed": w, "base": o}
            n += 1
    return edits, n


# ---------------------------------------------------------------- SSML


def _xml(t: str) -> str:
    import html
    return html.escape(t, quote=False)


_SENT_RE = re.compile(r"(?<=[.!?…»])\s+(?=[«—А-ЯЁ])")


def _sentences(t: str):
    return [x for x in _SENT_RE.split(t) if x]


# Выделение голосом. Из текста не узнать, надо ли произносить вопрос с нажимом
# («ПОЧЕМУ копии нет?» против «почему копии нет?»), и придумывать за автора
# неправильно. Но маркер у автора уже есть -- заглавные буквы. Их и читаем:
# слово капсом произносится выше и медленнее, как выделяют голосом в разговоре.
# Аббревиатуры (ГЭС, НИИ) не трогаем -- отсюда нижняя граница в четыре буквы.
EMPHASIS = 'pitch="x-high" rate="slow"'
# В ВОПРОСЕ замедления быть не должно: вопрос без вопросительного слова держится
# на подъёме тона («Он завтра БУДЕТ знать?»), и растянутое слово этот подъём
# гасит. На слух 2026-09-20: подъём без замедления выбран из десяти вариантов.
EMPHASIS_Q = 'pitch="x-high"'
QUESTION_END = re.compile(r'\?["»\'\)\]\s]*$')
MIN_CAPS_LEN = 3
CYR_UPPER = set("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
# короткие слова капсом бывают и аббревиатурами -- эти читаем как обычно
ABBREVIATIONS = set("""
ГЭС ТЭЦ АЭС ГРЭС НИИ СССР США ООН ЕС РФ ГОСТ ВУЗ ЗАГС МЧС ФСБ КГБ МВД ГИБДД
ПТУ ЖКХ СМИ ТВ ГСМ КПП ЛЭП ЧП ЭВМ ЭКГ УЗИ МРТ ДНК РНК
""".split())


def _is_caps(w: str, min_len: int = 2) -> bool:
    plain = w.replace("+", "")
    return (len(plain) >= min_len and plain.isupper()
            and all(ch in CYR_UPPER for ch in plain))


def _is_caps_word(w: str) -> bool:
    """Слово, набранное капсом ради выделения, а не аббревиатура."""
    plain = w.replace("+", "")
    return _is_caps(w, MIN_CAPS_LEN) and plain not in ABBREVIATIONS


def emphasize_caps(escaped: str) -> str:
    """Слова капсом -> prosody. Если капсом набрано полпредложения и больше,
    это уже не выделение отдельного слова, а крик -- поднимаем всю фразу
    (в крике много коротких слов, поэтому здесь считаем их все)."""
    words = [w for w in WORD_RE.findall(escaped) if len(w.replace("+", "")) > 1]
    if not words:
        return escaped
    emph = EMPHASIS_Q if QUESTION_END.search(escaped) else EMPHASIS
    shouted = [w for w in words if _is_caps(w) and w.replace("+", "") not in ABBREVIATIONS]
    if shouted and len(shouted) * 2 >= len(words):
        return f"<prosody {emph}>{escaped}</prosody>"
    if not any(_is_caps_word(w) for w in words):
        return escaped
    return WORD_RE.sub(
        lambda m: (f"<prosody {emph}>{m.group(0)}</prosody>"
                   if _is_caps_word(m.group(0)) else m.group(0)), escaped)


def add_pauses(escaped: str) -> str:
    """Паузы там, где их даёт не пунктуация, а вид текста.

    Скобки и кавычки модель выбрасывает вместе с их интонацией: вставка
    в скобках сливается с фразой, а закавыченная буква («е» или «ё»)
    проскакивает так, что её не разобрать. Ставим короткие паузы сами."""
    out = re.sub(r"\(", '<break time="200ms"/>(', escaped)
    out = re.sub(r"\)", ')<break time="200ms"/>', out)
    # одиночная буква в кавычках: «е», «ё» -- иначе проскакивает неразличимо
    out = re.sub(r"([«\"'])([^«»\"']{1})([»\"'])",
                 r'<break time="150ms"/>\1\2\3<break time="150ms"/>', out)
    return out


# ровно одна буква в кавычках: «да» -- это уже реплика, её делить не нужно
_LETTER_THEN_COMMA = re.compile(r"([«\"'][^«»\"']{1}[»\"'])\s*,\s*")


# Вопросительные слова во всех падежах. Вопрос С таким словом произносится
# с ПОНИЖЕНИЕМ на нём («Что́ он сказал?»), подъём в конце был бы ошибкой;
# вопрос БЕЗ него держится только на подъёме и иначе слышится утверждением.
QUESTION_WORD = re.compile(
    r"\b(чт\+?о|чег\+?о|чем\+?у|ч\+?ем|ч\+?ём|к\+?ак|как\+?ой|как\+?ая|как\+?ое"
    r"|как\+?ие|как\+?ог\+?о|как\+?ому|как\+?им|как\+?их|как\+?ов|наск\+?олько"
    r"|гд\+?е|куд\+?а|отк\+?уда|когд\+?а|кт\+?о|ког\+?о|ком\+?у|к\+?ем|к\+?ом"
    r"|зач\+?ем|почем\+?у|отчег\+?о|ск\+?олько|ч\+?ей|чь\+?я|чь\+?ё|чь\+?и|ли)\b",
    re.IGNORECASE)
# Последнее слово перед знаком вопроса -- на нём и делается подъём.
_LAST_BEFORE_Q = re.compile(r"([А-Яа-яЁё+]+)(\?[\"»'\)\]\s]*)$")


def question_rise(escaped: str) -> str:
    """Подъём тона на последнем слове вопроса без вопросительного слова.

    Русская ИК-3: «Ты пойдёшь?», «Он завтра будет знать, как решать?» --
    без подъёма модель читает их утверждением (на слух 2026-09-20, подъём
    выиграл или сравнялся во всех шести пробах).

    Не трогаем: вопросы с вопросительным словом (там понижение) и те, где
    автор уже выделил слово капсом -- его выбор важнее общего правила."""
    if not QUESTION_END.search(escaped) or "<prosody" in escaped:
        return escaped
    plain = re.sub(r"<[^>]+>", "", escaped)
    # Сам вопрос -- это то, что после последней кавычки, скобки или двоеточия:
    # в «Тихон спрашивает Аню: «Что во мне такого?»» начало фразы -- слова
    # автора, а вопросительное слово стоит уже в реплике.
    start = max(plain.rfind(c) for c in '«"(:')
    question = plain[start + 1:] if start >= 0 else plain
    # Внутри вопроса слово ищем в ПЕРВОЙ его части: в «Он завтра будет знать,
    # как решать?» слово «как» сидит в придаточном, а сам вопрос начинается
    # с «Он», и подъём ему нужен.
    if QUESTION_WORD.search(re.split(r"[,;]", question, 1)[0]):
        return escaped
    return _LAST_BEFORE_Q.sub(r'<prosody pitch="x-high">\1</prosody>\2', escaped)


def split_after_letter(escaped: str) -> str:
    """Закончить предложение после закавыченной буквы.

    «где решается «е» или «ё», жёлтые — где ударение» — на слух «ё»
    пропадает: перед похожим слогом у неё нет слышимой границы. Точка
    помогает, но менять запятую в тексте нельзя. Зато можно закрыть здесь
    предложение в разметке: модель даст завершающую интонацию, а текст
    останется авторским."""
    return _LETTER_THEN_COMMA.sub(r"\1</s><s>", escaped)


def ssml_paragraph(text: str, rate=None, pitch=None) -> str:
    # порядок важен: границу предложения ищем по чистому тексту, паузы
    # добавляем после -- иначе вставленные теги разрывают шаблон
    body = "".join(f"<s>{add_pauses(split_after_letter(question_rise(emphasize_caps(_xml(x)))))}</s>"
                   for x in _sentences(text))
    if rate or pitch:
        attrs = (f' rate="{rate}"' if rate else "") + (f' pitch="{pitch}"' if pitch else "")
        body = f"<prosody{attrs}>{body}</prosody>"
    return f"<p>{body}</p>"


def ssml_title(text: str) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    inner = '<break time="500ms"/>'.join(f"<s>{_xml(p)}</s>" for p in parts)
    return f'<p><prosody rate="slow" pitch="low">{inner}</prosody></p>'


def build_ssml(text: str, kind: str, rate: str | None = None) -> str:
    if kind == "title":
        body = ssml_title(text)
    elif kind == "epigraph":
        body = ssml_paragraph(text, rate="slow")
    elif kind == "attrib":
        body = ssml_paragraph(text, rate="slow", pitch="low")
    else:
        body = ssml_paragraph(text, rate=rate)
    return f'<speak><break time="{LEADIN_MS}ms"/>{body}</speak>'


# ---------------------------------------------------------------- синтез


class Synth:
    """Silero TTS. Модель качается в ~/.cache/torch/hub при первом запуске."""

    def __init__(self, model_id: str = DEFAULT_MODEL, threads: int = 0, log=print):
        self.model_id = model_id
        self.threads = threads
        self.log = log
        self.model = None

    def load(self):
        if self.model is not None:
            return
        import torch
        self.log(f"Загрузка модели {self.model_id} ...")
        path = find_model_file(self.model_id)
        if path:
            # .pt -- это torch package: грузим прямо из файла, без hub и сети.
            # Читаем в память намеренно: внутри torch файл открывается
            # сишным fopen в системной кодировке, и путь с кириллицей
            # («...\Аудиокнига\models\...») роняет загрузку с errno 2.
            import io
            with open(path, "rb") as f:
                buf = io.BytesIO(f.read())
            model = torch.package.PackageImporter(buf).load_pickle("tts_models", "model")
        else:
            self.log("Файл модели не найден, качаю через torch.hub ...")
            model, _ = torch.hub.load(repo_or_dir="snakers4/silero-models",
                                      model="silero_tts", language="ru",
                                      speaker=self.model_id, trust_repo=True)
        model.to(torch.device("cpu"))
        n = self.threads or max(1, (os.cpu_count() or 2) // 2)
        torch.set_num_threads(n)
        self.model = model
        self.log(f"Модель готова, потоков CPU: {n}")

    def say(self, text: str, speaker: str, kind: str = "par",
            use_ssml: bool = True, rate: str | None = None):
        """Один фрагмент -> float32 numpy. Текст уже с ударениями."""
        import numpy as np
        self.load()
        if not (text or "").strip():
            return np.zeros(0, dtype="float32")
        text = glue_clitics(text)
        own = "+" in text        # наша разметка -> Silero ничего не трогает сам
        kw = dict(speaker=speaker, sample_rate=SAMPLE_RATE,
                  put_accent=not own, put_yo=not own)
        if self.model_id.startswith("v5"):
            kw.update(put_stress_homo=not own, put_yo_homo=not own,
                      stress_single_vowel=not own)
        if use_ssml:
            try:
                audio = self.model.apply_tts(ssml_text=build_ssml(text, kind, rate), **kw)
            except Exception as e:
                # Две разные беды, и обе лечатся здесь. Слишком длинный
                # фрагмент («probably it's too long») делим пополам по
                # предложениям и читаем по частям -- разметка сохраняется.
                # Если делить уже нечего (внутри одно предложение с латиницей,
                # которой нет в алфавите модели), читаем без разметки: обычный
                # текст она чистит сама.
                parts = split_long(text, max(150, len(text) // 2))
                if len(parts) > 1:
                    self.log(f"Фрагмент длинный ({len(text)} знаков), читаю "
                             f"по частям: {text[:50]}…")
                    return np.concatenate([self.say(p, speaker, kind, True, rate)
                                           for p in parts])
                self.log(f"SSML не разобрался ({e}); читаю фрагмент без разметки: "
                         f"{text[:60]}")
                audio = self.model.apply_tts(text=text, **kw)
        else:
            audio = self.model.apply_tts(text=text, **kw)
        return audio.numpy().astype(np.float32)


def silence(sec: float):
    import numpy as np
    return np.zeros(int(SAMPLE_RATE * sec), dtype="float32")


def write_wav(path: str, pcm):
    import numpy as np
    pcm16 = np.clip(pcm * 32767, -32768, 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm16.tobytes())


def trim_silence(pcm, keep_head: float = 0.0, keep_tail: float = 0.06,
                 level: float = 0.01):
    """Срезать тишину по краям куска, оставив указанный запас.

    Каждый вызов синтеза начинается с разгона (LEADIN_MS) и собственного
    «вдоха» модели -- около секунды, и в конце ещё четверть. На абзац это
    звучит естественно, но в аудиоспектакле реплика режется на куски по
    голосам, и тишина набегает на каждом стыке: у реплики из трёх частей
    выходило около трёх секунд вместо одной (измерено 2026-09-21, Денис
    услышал лишнюю паузу на смене голоса)."""
    import numpy as np
    if len(pcm) == 0:
        return pcm
    loud = np.abs(pcm) > level
    if not loud.any():
        return pcm[:0]
    i = max(0, int(loud.argmax()) - int(keep_head * SAMPLE_RATE))
    j = min(len(pcm), len(pcm) - int(loud[::-1].argmax())
            + int(keep_tail * SAMPLE_RATE))
    return pcm[i:j]


def read_wav(path: str):
    import numpy as np
    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32767.0


def shift_voice(pcm, k: float):
    """Поднять голос в k раз -- высоту ВМЕСТЕ с формантами.

    Детский голос отличается от взрослого не только высотой основного
    тона: у ребёнка короче голосовой тракт, то есть выше и форманты.
    Поэтому не «поднять тон», а пересэмплировать (asetrate поднимает и то,
    и другое, заодно ускоряя) и вернуть темп обратно (atempo).

    ЧЕСТНО О КАЧЕСТВЕ (на слух 2026-09-21): при k=1.28 женский голос
    звучит МУЛЬТЯШНО. Это не детский голос, а его подобие. Годится оно
    там, где роль в одну-две реплики и выбирать приходится между подобием
    и явно взрослой женщиной вместо шестилетней девочки. На главной роли
    те же 1.28 будут слышны как дефект все сорок реплик: допустимая мера
    искусственности прямо зависит от размера роли.

    ПОНИЖЕНИЕ (k < 1) выходит куда чище подъёма, потому что удлинённые
    форманты -- это просто более крупный говорящий, а не выдуманный.
    Оно решает и другую задачу: развести двух персонажей одной природы.
    Прохор и Тихон оба воспитанные машины и говорят друг с другом; один
    голос на разной высоте слышится как «одной породы, но разные» --
    выбрано 0.90 (на слух 2026-09-21, вся сцена целиком, а не реплика:
    в быстром обмене короткими фразами важно не спутать).

    Без ffmpeg возвращаем звук как есть -- пусть лучше прозвучит взрослым
    голосом, чем не прозвучит совсем."""
    import numpy as np
    ff = ffmpeg_exe()
    if not ff or abs(k - 1.0) < 0.01 or len(pcm) == 0:
        return pcm
    import tempfile
    d = tempfile.mkdtemp(prefix="voice_")
    src, dst = os.path.join(d, "a.wav"), os.path.join(d, "b.wav")
    try:
        write_wav(src, pcm)
        af = (f"asetrate={int(SAMPLE_RATE * k)},aresample={SAMPLE_RATE},"
              f"atempo={1 / k:.5f}")
        run_quiet([ff, "-y", "-i", src, "-af", af, dst])
        return read_wav(dst)
    except Exception:
        return pcm
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def to_mp3(wav: str, mp3: str, title: str, track: int, album: str = "",
           artist: str = "", loudnorm: bool = True, quality: str = "2",
           log=None) -> bool:
    """wav -> mp3 с нормализацией громкости аудиокниги (-18 LUFS, пик -2 dBTP)."""
    af = ["-af", "highpass=f=60,loudnorm=I=-18:TP=-2:LRA=11"] if loudnorm else []
    meta = ["-metadata", f"title={title}", "-metadata", f"track={track}"]
    if album:
        meta += ["-metadata", f"album={album}"]
    if artist:
        meta += ["-metadata", f"artist={artist}"]
    try:
        run_quiet([FFMPEG, "-y", "-loglevel", "error", "-i", wav, *af,
                   "-codec:a", "libmp3lame", "-q:a", quality, *meta, mp3])
        return True
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        if log:
            err = getattr(e, "stderr", b"") or b""
            log(f"ffmpeg не собрал mp3: {err.decode('utf-8', 'replace')[:200] or e}")
        return False


def merge_mp3(files, out_path: str, quality: str = "3", log=None) -> bool:
    lst = out_path + ".list.txt"
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for p in files:
                f.write("file '%s'\n" % os.path.abspath(p).replace("'", r"'\''"))
        run_quiet([FFMPEG, "-y", "-loglevel", "error", "-f", "concat",
                   "-safe", "0", "-i", lst, "-codec:a", "libmp3lame",
                   "-q:a", quality, out_path])
        return True
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        if log:
            err = getattr(e, "stderr", b"") or b""
            log(f"ffmpeg не склеил файлы: {err.decode('utf-8', 'replace')[:200] or e}")
        return False
    finally:
        try:
            os.remove(lst)
        except OSError:
            pass


def render_section(synth: Synth, items, speaker: str, opts: dict,
                   progress=None, cancel=None, play: dict = None):
    """Секция (список (вид, текст)) -> один массив звука.

    play -- раскладка аудиоспектакля: {"narrator": голос, "voices": {имя:
    голос}, "speakers": [кто говорит в каждом абзаце]}. Без неё всё читается
    одним голосом, как раньше, бит в бит."""
    import numpy as np
    p_par = opts.get("pause_par", 0.4)
    p_title = opts.get("pause_title", 1.4)
    p_brk = opts.get("pause_break", 1.2)
    use_ssml = opts.get("ssml", True)
    rate = opts.get("rate") or None

    units = [(k, t) for (k, t) in items if k != "brk"]
    total = max(1, len(units))
    parts = [silence(0.5)]
    done = 0
    for pos, (kind, txt) in enumerate(items):
        if cancel is not None and cancel.is_set():
            break
        if kind == "brk":
            parts.append(silence(p_brk))
            continue
        if kind == "title":
            parts += [synth.say(txt, speaker, "title", use_ssml), silence(p_title)]
        elif play is not None and is_dialogue(txt):
            # реплика: каждый кусок своим голосом, слова автора -- рассказчиком
            # speakers идёт ОДИН В ОДИН с items, включая «brk» и заголовки:
            # считать по номеру озвученного абзаца нельзя, заголовки собьют
            who = play["speakers"][pos] if pos < len(play["speakers"]) else None
            plan = voice_plan(txt, who, play["voices"],
                              play.get("narrator", speaker), play.get("shifts"))
            # Разгон и «вдох» модели нужны ОДИН РАЗ, в начале реплики: внутри
            # неё каждый кусок приносил бы ещё около секунды тишины, и на
            # смене голоса слышалась лишняя пауза. Поэтому у всех кусков,
            # кроме первого, голова срезается, а между ними ставится короткая
            # пауза -- такая, какая бывает в живой речи перед словами автора.
            gap = opts.get("pause_seg", 0.07)
            for m, (text, voice, k) in enumerate(plan):
                if cancel is not None and cancel.is_set():
                    break
                for c, chunk in enumerate(split_long(text)):
                    said = synth.say(chunk, voice, kind, use_ssml, rate)
                    if k != 1.0:
                        said = shift_voice(said, k)
                    if m or c:
                        said = trim_silence(said)
                    parts.append(said)
                if m < len(plan) - 1:
                    parts.append(silence(gap))
            parts.append(silence(p_par))
        else:
            voice = play.get("narrator", speaker) if play is not None else speaker
            for chunk in split_long(txt):
                if cancel is not None and cancel.is_set():
                    break
                parts.append(synth.say(chunk, voice, kind, use_ssml, rate))
            parts.append(silence(p_par))
        done += 1
        if progress:
            progress(done, total, txt or "")
    return np.concatenate(parts)


# ------------------------------------------------------------ аудиоспектакль
#
# Обычная аудиокнига читается одним голосом, и это норма жанра. Но в «Пороге»
# 56% абзацев -- прямая речь, и на слух многоголосие оказалось лучше (проба
# 2026-09-21, сцена в мастерской на четверых). Поэтому режим необязательный:
# выключен -- всё как раньше, бит в бит.
#
# Русская проза размечает прямую речь тире в начале абзаца, а слова автора
# внутри реплики -- вторым тире: «— Пойдём, — сказал он, — уже поздно». Это
# машиночитаемо, и разбор надёжен. А вот КТО говорит, из текста следует далеко
# не всегда: имя названо лишь в 37% реплик.

DASHES = "—–"

# Глаголы речи и действия, по которым узнаются слова автора. Список рабочий,
# а не исчерпывающий: важно отличить их от продолжения реплики.
SPEECH_VERB = (
    # собственно речь
    r"сказа\w*|говор\w*|спроси\w*|отвеча\w*|ответи\w*|произн\w*|повтори\w*|"
    r"добави\w*|замети\w*|возрази\w*|отозва\w*|буркн\w*|крикн\w*|шепн\w*|"
    r"шепта\w*|пробормота\w*|переби\w*|продолжи\w*|позва\w*|окликн\w*|"
    r"согласи\w*|подтверди\w*|призна\w*|поясни\w*|объясни\w*|уточни\w*|"
    r"напомни\w*|предложи\w*|попроси\w*|вел\w*|приказа\w*|проворча\w*|"
    r"протян\w*|выдохн\w*|начал\w*|закончи\w*|заключи\w*|переспроси\w*|"
    # несовершенный вид: «спрашивал» под «спроси\w*» не подходит, и слова
    # автора терялись целиком вместе с именем говорящего
    r"спрашива\w*|переспрашива\w*|повторя\w*|добавля\w*|замеча\w*|"
    r"возража\w*|соглаша\w*|подтвержда\w*|поясня\w*|объясня\w*|уточня\w*|"
    r"напомина\w*|предлага\w*|проси\w*|перебива\w*|продолжа\w*|"
    r"приказыва\w*|заканчива\w*|кива\w*|кача\w*|"
    # книжные и разговорные глаголы речи -- в этой повести их нет, но в
    # другой книге без них потеряется имя говорящего
    r"вопроша\w*|интересова\w*|осведоми\w*|воскликн\w*|вскрикн\w*|"
    r"промолви\w*|молви\w*|процеди\w*|прошипе\w*|рявкн\w*|гаркн\w*|"
    r"забормота\w*|прошепта\w*|хохотн\w*|усмеха\w*|уверя\w*|убежда\w*|"
    r"настаива\w*|обеща\w*|посовет\w*|предупреди\w*|пожалова\w*|заяви\w*|"
    r"откликн\w*|поправи\w*|подхвати\w*|спохвати\w*|затороп\w*|"
    # действие вместо речи: «— Нет, — покачал он головой»
    r"усмехн\w*|кивн\w*|поиска\w*|помолча\w*|вздохн\w*|посмотре\w*|подума\w*|"
    r"засмея\w*|улыбн\w*|покача\w*|хмыкн\w*|фыркн\w*|пожа\w*|рассмея\w*|"
    r"погляде\w*|глян\w*|нахмури\w*|охн\w*|ахн\w*")
_SPEECH_VERB_RE = re.compile(rf"\b(?:{SPEECH_VERB})\b", re.IGNORECASE)
_SEG_SPLIT_RE = re.compile(rf"\s+[{DASHES}]\s+")
_NAME_AFTER_VERB = re.compile(rf"(?:{SPEECH_VERB})\s+((?:[А-ЯЁ][а-яё]+\s*){{1,3}})")
_NAME_BEFORE_VERB = re.compile(rf"((?:[А-ЯЁ][а-яё]+\s*){{1,3}})(?:{SPEECH_VERB})")
# «Дальский положил на стол большую руку» -- слова автора без глагола речи.
# Имя собственное плюс прошедшее время: речь с такого начала не бывает.
_NAME_THEN_PAST = re.compile(
    r"^[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+){0,2}\s+[а-яё]+л(?:а|о|и|ся|ась|ись)?\b")


# Знак ударения для ЧТЕНИЯ ГЛАЗАМИ. В файлах и в синтезе ударение живёт как
# «+» перед гласной: на клавиатуре ударных букв нет, и править текст удобнее
# плюсом. Но читать «к+ошка» тяжелее, чем «ко́шка», поэтому в окне и в бегущей
# строке показываем привычный надстрочный знак.
#
# Перевод ТОЧНО обратим и не меняет длину строки: «+о» -- два символа, «о» с
# комбинирующим ударением -- тоже два. Благодаря этому все позиции в тексте
# (подсветка, координаты спорных мест, выделение слова) остаются верными.
ACUTE = "́"
_PLUS_VOWEL = re.compile(r"\+([аеёиоуыэюяАЕЁИОУЫЭЮЯ])")
_VOWEL_ACUTE = re.compile(rf"([аеёиоуыэюяАЕЁИОУЫЭЮЯ]){ACUTE}")


def to_accent_marks(text: str) -> str:
    """«к+ошка» -> «ко́шка» (для показа человеку)."""
    return _PLUS_VOWEL.sub(rf"\1{ACUTE}", text)


def from_accent_marks(text: str) -> str:
    """«ко́шка» -> «к+ошка» (обратно, для файла и синтеза)."""
    return _VOWEL_ACUTE.sub(r"+\1", text)


def unstress(text: str) -> str:
    """Текст без знаков ударения.

    Нужен всюду, где мы ищем слова шаблоном: в размеченном тексте «сказала»
    выглядит как «сказ+ала», и ни один шаблон глагола не совпадёт."""
    return text.replace("+", "")


def is_dialogue(line) -> bool:
    # у разделителя сцен текста нет вовсе -- отсюда проверка на пустоту
    return bool(line) and line.lstrip().startswith(tuple(DASHES))


def split_reply(line: str) -> list:
    """Реплика -> [("speech"|"author", текст)].

    Куски чередуются, начиная с речи. Но тире бывает и знаком препинания
    внутри самой реплики («Порог — это место или звук?»), поэтому кусок
    считается словами автора только если в нём есть глагол речи или
    действия; иначе он приклеивается обратно к речи.

    Список глаголов всех не покроет, поэтому есть и второй признак: кусок
    начинается с имени собственного и прошедшего глагола («Дальский
    положил на стол большую руку»). Речь так не начинается -- человек не
    говорит о себе в третьем лице, -- а слова автора так начинаются
    постоянно, с любым глаголом."""
    body = line.lstrip().lstrip(DASHES + " ")
    out, speech = [], True
    for part in _SEG_SPLIT_RE.split(body):
        if not part.strip():
            continue
        if (not speech and not _SPEECH_VERB_RE.search(unstress(part))
                and not _NAME_THEN_PAST.match(unstress(part))):
            kind, prev = out[-1]
            out[-1] = (kind, prev + " — " + part.strip())
            continue
        out.append(("author" if not speech else "speech", part.strip()))
        speech = not speech
    return out


def verb_gender(chunk: str) -> str | None:
    """Род говорящего по форме глагола: «сказал» -- м, «сказала» -- ж.

    Глагол прошедшего времени согласован с подлежащим, то есть с самим
    говорящим, и это готовая подсказка прямо в тексте -- никакого словаря
    имён не нужно. Множественное число («сказали») пропускаем: там
    говорящих несколько."""
    m = re.search(rf"\b({SPEECH_VERB})\b", unstress(chunk), re.IGNORECASE)
    if not m:
        return None
    v = m.group(0).lower()
    if v.endswith("ли"):
        return None
    if v.endswith("ла") or v.endswith("лась"):
        return "ж"
    if v.endswith("л") or v.endswith("лся"):
        return "м"
    return None


def detect_genders(lines: list, cast) -> dict:
    """Род каждого персонажа -- по глаголам, которыми его вводит автор.

    «— сказала Аня» встречается чаще, чем ошибается: берём преобладающий
    род по всем упоминаниям."""
    votes = {}
    for line in lines:
        if not is_dialogue(line):
            continue
        for kind, chunk in split_reply(line):
            if kind != "author":
                continue
            who = speaker_from_author(chunk, cast)
            g = verb_gender(chunk)
            if who and g:
                votes.setdefault(who, {"м": 0, "ж": 0})[g] += 1
    return {who: ("ж" if v["ж"] > v["м"] else "м")
            for who, v in votes.items() if v["м"] != v["ж"]}


def speaker_from_lead(text: str, cast, genders: dict = None,
                      present: list = None) -> str | None:
    """Говорящий по ПРЕДЫДУЩЕМУ абзацу, если тот кончается двоеточием.

    «Матвей Ильич отложил рубанок, взял доску за оба конца, поднял,
    повернул на свету и сказал:» -- дальше идёт реплика, и кто её
    произносит, сказано прямо, только не внутри неё (замечено Денисом
    2026-09-21 при разборе говорящих).

    Имя берём не рядом с глаголом -- глагол стоит в конце, а подлежащее
    в начале, между ними полстроки, -- а из всего абзаца, сверяя род с
    формой глагола: «Когда Аня ушла, Матвей Ильич сказал:» иначе дало бы
    Аню. Если имени нет вовсе («он сел на козлы и сказал:»), решаем по
    роду среди тех, кто в этой сцене уже говорил."""
    plain = unstress(text).rstrip()
    if not plain.endswith(":"):
        return None
    # Глагол берём ПОСЛЕДНИЙ -- тот, что перед двоеточием: в абзаце их
    # бывает несколько, и «Кузьма объяснял… а Тихон слушал и спросил:»
    # по первому дало бы Кузьму с его «объяснял».
    хвост = list(_SPEECH_VERB_RE.finditer(plain))
    if not хвост:
        return None
    конец = хвост[-1]
    g = verb_gender(конец.group(0))
    # Имя -- ближайшее СЛЕВА от этого глагола: подлежащее стоит перед ним,
    # а прочие имена в абзаце к нему отношения не имеют.
    слева = []
    for name, варианты in _cast_forms(cast).items():
        for f in варианты:
            for m in re.finditer(rf"\b{re.escape(f)}[а-яё]*\b",
                                 plain[:конец.start()]):
                слева.append((m.start(), name))
    слева.sort(reverse=True)
    if слева:
        подходят = [n for _, n in слева if not g or (genders or {}).get(n) == g]
        if подходят:
            return подходят[0]
        if not g:
            return слева[0][1]
    if g and present:
        подходят = [n for n in present if (genders or {}).get(n) == g]
        if len(подходят) == 1:
            return подходят[0]
    return None


# Местоимение должно быть ПОДЛЕЖАЩИМ при глаголе речи: «сказал он», «она
# ответила». Просто «он» где-то в словах автора не годится -- в «сказал шар.
# Голос был тот же... теперь он звучал совсем близко» это «он» про голос, а
# говорящий назван, только он не из списка ролей.
PRONOUN_HE_SHE = re.compile(
    rf"(?:{SPEECH_VERB})\s+(?:он|она)\b|\b(?:он|она)\s+(?:{SPEECH_VERB})",
    re.IGNORECASE)


def speaker_from_pronoun(before: list, cast, genders: dict, chunk: str):
    """Кто такой «он» в «— Ильич, — сказал он».

    Имя названо в предыдущем абзаце: «Первым увидел Кузьма. Он вообще всё
    видел первым... — Ильич, — сказал он» (пример Дениса 2026-09-21).
    Берём ближайшее к концу повествования имя подходящего рода.

    Имя ищется ТОЛЬКО в именительном падеже, буква в букву. Косвенный
    падеж -- это почти всегда не тот, кто действует, а тот, кому: в
    «отдал Тихону колесо и встал рядом. -- Держи, -- сказал он» говорит
    не Тихон, а тот, кто отдал; в «ответил не Фёдору, а Ане» -- тем
    более. Правило держится на том, что подлежащее стоит в именительном,
    и на проверке это решило четыре ошибки из пяти.

    Возвращает (имя, точно ли): точно -- когда в том абзаце вообще один
    человек этого рода, иначе это выбор из нескольких и лучше пометить
    догадкой."""
    род = verb_gender(chunk)
    if not род or not PRONOUN_HE_SHE.search(unstress(chunk)):
        return None, False
    for текст in before:
        unstype = unstress(текст or "")
        места = []
        for имя, формы in _cast_forms(cast).items():
            if genders.get(имя) != род:
                continue
            for f in формы:
                все = list(re.finditer(rf"\b{re.escape(f)}\b", unstype,
                                       re.IGNORECASE))
                if все:
                    места.append((все[-1].start(), имя))
        if места:
            места.sort()
            кто = места[-1][1]
            return кто, len({и for _, и in места}) == 1
    return None, False


def _cast_forms(cast) -> dict:
    """Роли в общем виде {имя: (как его называют в тексте, ...)}.

    Принимаем и простой список имён, и словарь «имя -> голос»: в обоих
    случаях имя ищется само по себе."""
    if isinstance(cast, dict) and cast and isinstance(next(iter(cast.values())),
                                                      (list, tuple, set)):
        return cast
    return {name: (name,) for name in cast}


def speaker_from_author(chunk: str, cast) -> str | None:
    """Кто говорит, по словам автора: имя рядом с глаголом речи.

    Важно смотреть именно рядом с глаголом: в «— Тихон говорит, ты его
    учишь, — сказал Матвей» первое имя принадлежит не говорящему.

    Если глагола речи в куске нет вовсе (слова автора узнаны по началу
    «Имя + прошедшее время»), то говорящий -- только это начальное имя, и
    больше искать негде: в «— Голос шёл не из динамика, а из глаза...»
    имя Матвея встречается в середине, но говорит не он."""
    text = unstress(chunk)
    if not _SPEECH_VERB_RE.search(text):
        m = _NAME_THEN_PAST.match(text)
        if not m:
            return None
        начало = m.group(0)
        свои = [(n, f) for n, ff in _cast_forms(cast).items() for f in ff
                if re.match(rf"\b{re.escape(f)}\b", начало)]
        return max(свои, key=lambda p: len(p[1]))[0] if свои else None
    m = _NAME_AFTER_VERB.search(text) or _NAME_BEFORE_VERB.search(text)
    hay = m.group(1) if m else text
    # Берём имя, стоящее РАНЬШЕ прочих: «Матвей Ильич» -- это Матвей, а
    # перебор в произвольном порядке (cast бывает множеством) давал то одно,
    # то другое от запуска к запуску.
    best = None
    for name, forms in _cast_forms(cast).items():
        for form in forms:
            hit = re.search(rf"\b{re.escape(form)}[а-яё]*\b", hay)
            if hit and (best is None or hit.start() < best[0]
                        or (hit.start() == best[0] and len(form) > best[1])):
                best = (hit.start(), len(form), name)
    return best[2] if best else None


NARRATION_BREAK = 2      # столько абзацев повествования подряд рвут разговор


def _scene_bounds(items: list) -> list:
    """Границы сцен: заголовок, разделитель «***» и длинный кусок
    повествования разговор прерывают.

    Это важно для чередования: оно считается ВНУТРИ сцены и включается
    только когда говорящих ровно двое. Сцена между разделителями бывает
    длиной в тридцать абзацев -- Кузьма говорит в одном её конце, Аня с
    дедом в другом, -- и разговор двоих оставался неразобранным из-за
    третьего, которого рядом нет (замечено Денисом 2026-09-21:
    «— Спрашивай. — А зачем ты её строишь?»).

    Два и более абзаца повествования подряд -- признак, что разговор
    кончился. Проверено на спрятанных якорях: с таким делением 91 догадка
    при 65% точности против 90 при 63% без него; по одному абзацу рвать
    нельзя (слова автора между репликами -- ещё не конец разговора), по
    трём -- хуже."""
    edges, start, plain = [], 0, 0
    for i, it in enumerate(items):
        kind = it[0] if isinstance(it, (tuple, list)) else None
        text = it[1] if isinstance(it, (tuple, list)) else it
        if kind in ("brk", "title"):
            if i > start:
                edges.append((start, i))
            start, plain = i + 1, 0
            continue
        if is_dialogue(text):
            plain = 0
            continue
        plain += 1
        if plain == NARRATION_BREAK:
            конец = i - NARRATION_BREAK + 1
            if конец > start:
                edges.append((start, конец))
            start = i + 1
        elif plain > NARRATION_BREAK:
            start = i + 1
    if start < len(items):
        edges.append((start, len(items)))
    return edges


def guess_speakers(items: list, cast: dict, known: dict = None,
                   genders: dict = None) -> list:
    """Обёртка: режет на сцены и разбирает каждую отдельно.

    items -- либо строки, либо пары (вид, текст), как их отдаёт parse_text;
    во втором случае границы сцен берутся из видов. Род персонажей, если
    не передан, выводится из текста сам."""
    paired = bool(items) and isinstance(items[0], (tuple, list))
    texts = [it[1] for it in items] if paired else list(items)
    if genders is None:
        genders = detect_genders(texts, cast)
    out = [None] * len(texts)
    for a, b in (_scene_bounds(items) if paired else [(0, len(texts))]):
        for k, res in zip(range(a, b),
                          _guess_scene(texts[a:b], cast, known, genders,
                                       texts[max(0, a - 4):a])):
            out[k] = res
    return out


def _guess_scene(lines: list, cast: dict, known: dict = None,
                 genders: dict = None, before: list = None) -> list:
    """Кто говорит в каждой реплике сцены.

    Возвращает список той же длины, что lines: None для повествования,
    иначе (имя|None, уверенность), где уверенность -- "точно" (имя названо
    или проставлено человеком), "догадка" (выведено чередованием) или
    "нет" (сказать нечего).

    Чередование работает только в сцене на ДВОИХ: там реплики идут по
    очереди, и от якоря с именем можно считать в обе стороны. Точность
    измерена на повести -- 81%. В сцене на троих и больше чередование
    бессмысленно, и такие реплики остаются без говорящего: пусть их
    назначит человек, чем машина уверенно соврёт.

    before -- несколько абзацев ПЕРЕД сценой. Сцену обрывают два абзаца
    повествования подряд, а говорящего следующей реплики называет как раз
    последний из них («Первым увидел Кузьма... — Ильич, — сказал он»), так
    что правилам «посмотреть назад» одной сцены мало."""
    known = known or {}
    out = [None] * len(lines)
    idx = [i for i, l in enumerate(lines) if is_dialogue(l)]
    before = list(before or [])
    сдвиг = len(before)
    контекст = before + list(lines)        # сцена вместе с подводкой к ней

    anchors = {}
    for i in idx:
        by_hand = known.get(line_key(unstress(lines[i])))
        if by_hand:
            anchors[i] = by_hand
            out[i] = (by_hand, "точно")
            continue
        for kind, chunk in split_reply(lines[i]):
            if kind == "author":
                who = speaker_from_author(chunk, cast)
                if who:
                    anchors[i] = who
                    out[i] = (who, "точно")
                    break
        else:
            out[i] = (None, "нет")

    # Второй источник якорей: предыдущий абзац, кончающийся двоеточием.
    # «Матвей Ильич ... и сказал:» -- говорящий назван прямо, только не
    # внутри реплики. Считаем это точным: тут не догадка, а указание.
    for i in idx:
        if out[i][1] == "точно" or i + сдвиг == 0:
            continue
        who = speaker_from_lead(контекст[сдвиг + i - 1], cast, genders,
                                list(dict.fromkeys(anchors.values())))
        if who:
            anchors[i] = who
            out[i] = (who, "точно")

    # Третий источник: местоимение в словах автора. «— Ильич, — сказал он»
    # имени не называет, но предыдущий абзац его называет: «Первым увидел
    # Кузьма. Он вообще всё видел первым...». Берём ближайшее имя нужного
    # рода; если в том абзаце человек этого рода один -- это не догадка.
    for i in idx:
        if out[i][1] == "точно" or i + сдвиг == 0:
            continue
        куски = [c for k, c in split_reply(lines[i]) if k == "author"]
        if not куски:
            continue
        n = сдвиг + i
        назад = [контекст[j] for j in range(n - 1, max(-1, n - 4), -1)
                 if not is_dialogue(контекст[j])]
        кто, точно = speaker_from_pronoun(назад, cast, genders or {},
                                          " ".join(куски))
        if кто:
            out[i] = (кто, "точно" if точно else "догадка")
            if точно:
                anchors[i] = кто

    # Четвёртый источник: род глагола. «— сказала она» не называет имени,
    # но глагол согласован с говорящим, и если в сцене женщина одна, говорящий
    # определён точно, а не догадкой. Так спасаются реплики с «он/она».
    present = list(dict.fromkeys(anchors.values()))
    if genders and len(present) > 1:
        for i in idx:
            if out[i][1] == "точно":
                continue
            for kind, chunk in split_reply(lines[i]):
                if kind != "author":
                    continue
                g = verb_gender(chunk)
                if not g:
                    continue
                fits = [n for n in present if genders.get(n) == g]
                if len(fits) == 1:
                    anchors[i] = fits[0]
                    out[i] = (fits[0], "точно")
                break

    # Чередование включаем, только если в куске ровно ДВОЕ говорящих.
    #
    # Соблазнительно чередовать между соседними якорями и в многолюдной
    # сцене, но проверка это отвергла: на спрятанных якорях локальное
    # правило даёт 39% против 63% у строгого (2026-09-21). Причина в том,
    # что в этой прозе 24% подряд идущих реплик произносит ОДИН И ТОТ ЖЕ
    # человек -- между ними вставлено повествование, а говорящий
    # продолжает. Для выбора из двоих 39% -- хуже монетки.
    voices = list(dict.fromkeys(anchors.values()))
    if len(voices) != 2 or not anchors:
        return out

    a, b = voices
    other = {a: b, b: a}
    pos = {i: n for n, i in enumerate(idx)}          # номер реплики по порядку
    for i in idx:
        if out[i][1] == "точно":
            continue
        near = min(anchors, key=lambda k: abs(pos[k] - pos[i]))
        step = abs(pos[near] - pos[i])
        who = anchors[near] if step % 2 == 0 else other[anchors[near]]
        out[i] = (who, "догадка")

    # Последняя проверка: назначенный НЕ ДОЛЖЕН противоречить роду глагола.
    # «— Проголосовали, — сказала она» доставалась Кузьме -- чередование
    # не смотрит на то, что глагол женский. Лучше оставить без говорящего,
    # чем отдать реплику заведомо не тому.
    if genders:
        for i in idx:
            кто, как = out[i]
            if not кто or как == "точно":
                continue
            куски = [c for k, c in split_reply(lines[i]) if k == "author"]
            род = verb_gender(" ".join(куски)) if куски else None
            if род and genders.get(кто) and genders[кто] != род:
                out[i] = (None, "нет")
    return out


def proper_names(lines: list) -> set:
    """Имена собственные книги -- по самой книге, без словарей и морфологии.

    Признак простой и надёжный: имя собственное пишется с большой буквы
    ВЕЗДЕ, а обычное слово -- только в начале предложения. Поэтому берём
    слова, которые хоть раз встретились с большой буквы в СЕРЕДИНЕ фразы.

    Отсеивать наоборот, списком «не-имён», бессмысленно: с большой буквы
    в начале предложения бывает любое слово."""
    found = set()
    for line in lines:
        text = unstress(line or "")
        # начало предложения: после точки, восклицательного, вопросительного,
        # многоточия, открывающей кавычки или тире прямой речи
        for m in re.finditer(r"[А-ЯЁ][а-яё]{2,}", text):
            # Закрывающие кавычки и скобки сами по себе конца фразы не
            # значат, но и не отменяют его: «…скопировать?» Она не знает» --
            # предложение кончилось знаком ВНУТРИ кавычки, а «Она» начинает
            # новое. Поэтому смотрим на знак перед закрывающей кавычкой.
            before = text[:m.start()].rstrip().rstrip("»\"')]")
            if not before:
                continue                      # первое слово строки
            if before[-1] in ".!?…:«\"'([-—–":
                continue                      # первое слово предложения
            found.add(m.group(0))
    return found


def _name_stem(name: str) -> str:
    """Основа имени без падежного окончания: «Аня», «Ане», «Аню» -> «Ан»."""
    return name.rstrip("аеёийоуыэюя").lower()


def merge_name_forms(counts: dict) -> dict:
    """Склеить падежные формы одного имени в одну запись.

    «— сказал Ане» -- это дательный, то есть АДРЕСАТ, а не говорящий; но
    попадает такое в список как отдельный персонаж «Ане». Отличить падеж
    без морфологии нельзя, зато видно, что это форма уже известного имени:
    основы совпадают. Побеждает более частое написание -- обычно как раз
    именительный, которым автор вводит говорящего."""
    best = {}
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        stem = _name_stem(name)
        if len(stem) < 2:
            best[name] = best.get(name, 0) + n
            continue
        for kept in best:
            if _name_stem(kept) == stem:
                best[kept] += n
                break
        else:
            best[name] = n
    return dict(sorted(best.items(), key=lambda kv: -kv[1]))


def detect_characters(lines: list) -> dict:
    """Кто говорит в книге: имена из слов автора, с числом реплик.

    Берём имя рядом с глаголом речи -- «сказал Матвей Ильич», «Аня
    ответила» -- и оставляем от него ПЕРВОЕ слово. Так отчество и фамилия
    сливаются с именем сами: «Матвей Ильич» и «Матвей» -- один персонаж, а
    не два, и «Елена Карловна» не порождает лишнюю «Карловну».

    Именем считается только то, что книга сама показала как имя собственное
    (см. proper_names): иначе «Она сказала» даёт персонажа «Она»."""
    names = proper_names(lines)
    found = {}
    for line in lines:
        if not is_dialogue(line):
            continue
        for kind, chunk in split_reply(line):
            if kind != "author":
                continue
            text = unstress(chunk)
            m = _NAME_AFTER_VERB.search(text) or _NAME_BEFORE_VERB.search(text)
            if not m:
                continue
            words = [w for w in m.group(1).split()
                     if len(w) > 2 and w[0].isupper() and w in names]
            if words:
                found[words[0]] = found.get(words[0], 0) + 1
    return merge_name_forms(found)


def voices_path(acc_path: str) -> str:
    return acc_path + ".voices.json"


def load_voices(acc_path: str) -> dict:
    """Роли и назначения: {narrator, voices: {имя: голос}, speakers:
    {ключ строки: имя}}."""
    p = voices_path(acc_path)
    empty = {"narrator": "", "voices": {}, "speakers": {},
             "genders": {}, "shifts": {}}
    if not os.path.exists(p):
        return empty
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        empty.update({k: data.get(k, v) for k, v in empty.items()})
        return empty
    except Exception:
        return empty


def save_voices(acc_path: str, data: dict):
    p = voices_path(acc_path)
    if not any(data.get(k) for k in ("voices", "speakers",
                                     "genders", "shifts")):
        if os.path.exists(p):
            os.remove(p)
        return
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def voice_plan(line: str, speaker: str | None, cast_voices: dict,
               narrator: str, shifts: dict = None) -> list:
    """Строка -> [(текст, голос, подъём)].

    Слова автора внутри реплики читает РАССКАЗЧИК, а не персонаж: «— Он
    спросил, как пахнет смола, — сказала Аня. — Позавчера». Иначе Аня
    произносит «сказала Аня», и это слышно как ошибка. Подъём голоса
    (для детских ролей) относится только к словам персонажа."""
    shifts = shifts or {}
    if not is_dialogue(line):
        return [(line, narrator, 1.0)]
    voice = cast_voices.get(speaker or "", narrator)
    k = shifts.get(speaker or "", 1.0) if speaker in cast_voices else 1.0
    return [(text, narrator, 1.0) if kind == "author" else (text, voice, k)
            for kind, text in split_reply(line)]


# ---------------------------------------------------------------- словарь


# Словарь по умолчанию ПУСТ, и это осознанно. Словарь действует на слово везде,
# а правильное ударение часто зависит от места: правило «потом -> пот+ом»,
# верное для одной книги, испортит фразу «покрылся потом», которую автомат
# читает верно сам. Общими бывают только имена и термины конкретной книги --
# такие словари лежат в папке dictionaries и загружаются кнопкой.
# Для «слово читается не так ИМЕННО ЗДЕСЬ» есть ручные правки (см. выше).
DEFAULT_DICTIONARY = {}


def load_dictionary(path: str) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_DICTIONARY)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(DEFAULT_DICTIONARY)


def save_dictionary(path: str, d: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
