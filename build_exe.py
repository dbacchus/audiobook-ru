# -*- coding: utf-8 -*-
"""
Сборка автономного приложения: exe + модели рядом, без Python и без интернета.

    python build_exe.py            собрать
    python build_exe.py --zip      собрать и упаковать в архив

Получается папка `dist/Аудиокнига`, которую можно целиком скопировать на любой
Windows-компьютер. Внутри:

    Аудиокнига.exe        запуск
    _internal/            python, torch, tkinter -- всё, что нужно для работы
    models/v5_5_ru.pt     голосовая модель Silero (139 МБ)
    models/ruaccent/      модели ударений: омографы, словарь, е/ё (695 МБ)
    ffmpeg.exe            кодирование mp3 и громкость

Модели лежат СНАРУЖИ exe намеренно: их видно, можно заменить, и сборка от
этого не распухает. Пути к ним приложение берёт само (app_root в tts_core).
"""
import os
import shutil
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# Папку собираем с латинским именем, а exe внутри переименовываем в русское:
# torch открывает файлы моделей сишным fopen в системной кодировке, и путь с
# кириллицей роняет загрузку (errno 2). Имя самого exe при этом безопасно.
NAME = "Audiobook"
EXE_NAME = "Аудиокнига.exe"
DIST = os.path.join(APP_DIR, "dist", NAME)

# что в сборку не тащим: эти пакеты стоят в системе, но приложению не нужны
EXCLUDES = ["scipy", "pandas", "matplotlib", "vtk", "vtkmodules", "playwright",
            "IPython", "notebook", "pytest", "torchaudio", "torchvision",
            "tensorboard", "jedi", "docutils", "sqlalchemy", "cv2",
            # ffmpeg кладём рядом с exe отдельным файлом, второй экземпляр
            # внутри пакета -- это лишние 84 МБ; PIL приложению не нужен
            "imageio_ffmpeg", "imageio", "PIL"]
HIDDEN = ["ruaccent", "onnxruntime", "transformers", "torch.package",
          "tkinter", "tkinter.filedialog", "tkinter.messagebox"]


def mb(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1024 / 1024


def run_pyinstaller():
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--windowed", "--name", NAME, "--distpath", os.path.join(APP_DIR, "dist"),
           "--workpath", os.path.join(APP_DIR, "build"),
           "--specpath", APP_DIR]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    cmd.append(os.path.join(APP_DIR, "audiobook_app.py"))
    print("PyInstaller ...")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=APP_DIR)
    if r.returncode:
        sys.exit("PyInstaller завершился с ошибкой")
    print(f"  собрано за {time.time() - t0:.0f} c, {mb(DIST):.0f} МБ")


def copy_models():
    import tts_core as core
    models = os.path.join(DIST, "models")
    os.makedirs(models, exist_ok=True)

    # v5_cis_base -- под MIT, её можно свободно распространять. Специализированные
    # русские модели (v5_5_ru и т. п.) под CC BY-NC, только некоммерческое
    # использование: кладём их в сборку лишь по явному флагу --with-nc.
    wanted = ["v5_cis_base"] + (["v5_5_ru"] if "--with-nc" in sys.argv else [])
    for mid in wanted:
        src = core.find_model_file(mid)
        if not src:
            print(f"  ВНИМАНИЕ: {mid}.pt не найден, в сборку не попадёт")
            continue
        dst = os.path.join(models, mid + ".pt")
        if not os.path.exists(dst):
            print(f"  модель {mid}: {os.path.getsize(src)/1024/1024:.0f} МБ")
            shutil.copy2(src, dst)

    import ruaccent
    ru_src = os.path.dirname(os.path.abspath(ruaccent.__file__))
    # nn и dictionary RUAccent берёт из workdir -- кладём их рядом, на виду.
    for sub in ("nn", "dictionary"):
        s, d = os.path.join(ru_src, sub), os.path.join(models, "ruaccent", sub)
        if os.path.isdir(s) and not os.path.isdir(d):
            print(f"  модели RUAccent/{sub}: {mb(s):.0f} МБ")
            shutil.copytree(s, d)
    # а koziev (морфология) он ищет внутри своего пакета, workdir там не
    # используется -- значит копируем туда, где он его ждёт
    s = os.path.join(ru_src, "koziev")
    d = os.path.join(DIST, "_internal", "ruaccent", "koziev")
    if os.path.isdir(s) and not os.path.isdir(d):
        print(f"  модели RUAccent/koziev: {mb(s):.0f} МБ -> _internal")
        shutil.copytree(s, d)

    # ffmpeg в сборку кладём только по флагу: готовые сборки собраны под GPL,
    # и распространять их вместе с приложением нельзя без соблюдения условий.
    # Без флага приложение при первом запуске предложит скачать LGPL-сборку.
    if "--with-ffmpeg" in sys.argv:
        ff = core.ffmpeg_exe()
        if ff and os.path.exists(ff):
            shutil.copy2(ff, os.path.join(DIST, "ffmpeg.exe"))
            print(f"  ffmpeg: {os.path.getsize(ff)/1024/1024:.0f} МБ (GPL-сборка!)")
        else:
            print("  ВНИМАНИЕ: ffmpeg не найден")
    else:
        print("  ffmpeg: не включён, приложение предложит скачать LGPL-сборку")


QUICKSTART = """\
Аудиокнига {version}

ЗАПУСК: Аудиокнига.exe
Больше ничего устанавливать не нужно.

КАК ПОЛЬЗОВАТЬСЯ

1. Вкладка «1. Текст и ударения» -> «Добавить...» -> выберите главы книги
   в виде текстовых файлов (.txt или .md).

2. Нажмите «Расставить ударения». Первый раз 10-20 секунд грузятся модели
   (окно на это время замирает, так и надо), потом идёт расстановка.
   Рядом с каждой главой появится файл «глава.acc.txt» -- обычный текст,
   где знак + стоит перед ударной гласной. Его можно править руками.

3. Просмотрите подсвеченные места: сиреневые -- где решается «е» или «ё»,
   жёлтые -- где ударение меняет смысл. Внизу они же списком с контекстом.
   Двойной клик по слову -- поправить. Выделите абзац и нажмите
   «Прослушать выделенное», чтобы проверить на слух.

4. Вкладка «2. Озвучка» -> выберите голос («Образец голоса» даст послушать)
   -> «Озвучить все».

Готовые mp3 появятся в папке audio рядом с программой.

ЕСЛИ ЧТО-ТО НЕ ТАК

* При первой озвучке программа предложит скачать ffmpeg (73 МБ) -- без него
  главы сохранятся в WAV, без сжатия и выравнивания громкости.
* Меню «Справка» -> «О программе»: версия, лицензии и пути к моделям.
* Подробное описание -- в файле README.md.
* Ошибки записываются в app.log рядом с программой.

Идея, постановка задач и приёмка на слух -- dbacchus.
Код -- Claude (Anthropic) под его руководством. Лицензия MIT.
"""


def copy_extras():
    with open(os.path.join(DIST, "Прочти меня.txt"), "w", encoding="utf-8") as f:
        import tts_core as core
        f.write(QUICKSTART.format(version=core.VERSION))
    for name in ("README.md", "dictionary.json"):
        src = os.path.join(APP_DIR, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))
    src = os.path.join(APP_DIR, "dictionaries")
    dst = os.path.join(DIST, "dictionaries")
    if os.path.isdir(src) and not os.path.isdir(dst):
        shutil.copytree(src, dst)


def make_zip():
    """Архив для переноса. 7-Zip жмёт заметно лучше и быстрее обычного zip."""
    print("упаковка (это долго) ...")
    t0 = time.time()
    seven = next((p for p in (r"C:\Program Files\7-Zip\7z.exe",
                              r"C:\Program Files (x86)\7-Zip\7z.exe")
                  if os.path.exists(p)), None)
    if seven:
        archive = os.path.join(APP_DIR, "dist", NAME + ".7z")
        if os.path.exists(archive):
            os.remove(archive)
        subprocess.run([seven, "a", "-t7z", "-mx=7", "-mmt=on", archive, DIST],
                       cwd=os.path.join(APP_DIR, "dist"),
                       stdout=subprocess.DEVNULL, check=True)
    else:
        archive = shutil.make_archive(os.path.join(APP_DIR, "dist", NAME), "zip",
                                      root_dir=os.path.join(APP_DIR, "dist"),
                                      base_dir=NAME)
    size = os.path.getsize(archive) / 1024 / 1024
    print(f"  {archive}: {size:.0f} МБ за {time.time() - t0:.0f} c")


def rename_exe():
    src = os.path.join(DIST, NAME + ".exe")
    dst = os.path.join(DIST, EXE_NAME)
    if os.path.exists(src):
        if os.path.exists(dst):
            os.remove(dst)
        os.rename(src, dst)
        print(f"  запуск: {EXE_NAME}")


if __name__ == "__main__":
    sys.path.insert(0, APP_DIR)
    run_pyinstaller()
    print("модели и вспомогательные файлы ...")
    copy_models()
    copy_extras()
    rename_exe()
    print(f"\nГотово: {DIST}")
    print(f"Размер папки: {mb(DIST):.0f} МБ")
    if "--zip" in sys.argv:
        make_zip()
