# -*- coding: utf-8 -*-
"""
Аудиокнига — расстановка ударений и озвучка текстовых файлов (Silero + RUAccent).

Три шага, три вкладки:
    1. Текст и ударения — список глав, расстановка ударений, редактор с
       подсветкой омографов, прослушивание выделенного абзаца;
    2. Озвучка — голос, модель, темп, паузы, mp3;
    3. Словарь — слова, где автомат ошибается; словарь всегда побеждает.

Запуск:  python audiobook_app.py     (или ярлык «Аудиокнига.bat»)
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _CODE_DIR)
# Рабочая папка приложения: рядом с exe в собранной версии, рядом с кодом в
# обычной. В собранной __file__ указывает ВНУТРЬ _internal, и настройки,
# словарь, кэш и папка audio уезжали в служебную папку, где их не найти.
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable))
           if getattr(sys, "frozen", False) else _CODE_DIR)


def _redirect_std_streams():
    """Запуск под pythonw: у процесса нет консоли, sys.stdout и sys.stderr
    равны None, и первый же print внутри чужой библиотеки роняет её с
    «'NoneType' object has no attribute 'write'» (torch.hub печатает при
    загрузке модели). Отводим оба потока в файл журнала рядом с приложением --
    заодно там остаются трейсбеки, которые pythonw иначе проглатывает."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.path.join(APP_DIR, "app.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.remove(path)
        f = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        class _Null:
            def write(self, *a):
                pass

            def flush(self):
                pass

            def isatty(self):
                return False
        f = _Null()
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


_redirect_std_streams()


import tts_core as core
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
DICT_PATH = os.path.join(APP_DIR, "dictionary.json")
YO_LIST_PATH = os.path.join(APP_DIR, "yo_homographs.json")
ACC_SUFFIX = ".acc.txt"          # файл с расставленными ударениями


def safe_name(s: str, limit: int = 48) -> str:
    """Название главы -> имя файла: без знаков ударения и запрещённых символов."""
    s = re.sub(r'[\\/:*?"<>|]', "", (s or "").replace("+", "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:limit].rstrip(" .") or "chapter")

# Windows: чёткий шрифт на мониторе с масштабированием
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# ---------------------------------------------------------------- фоновые задачи

class Ctx:
    """То, что фоновая работа может сообщать в окно."""

    def __init__(self, q: queue.Queue, cancel: threading.Event):
        self.q = q
        self.cancel = cancel

    def log(self, text):
        self.q.put(("log", str(text)))

    def status(self, text):
        self.q.put(("status", str(text)))

    def busy(self, text):
        """Работа без счётчика: бегущая полоса, чтобы окно не выглядело мёртвым."""
        self.q.put(("busy", str(text)))

    def progress(self, cur, total, label=""):
        self.q.put(("progress", (cur, total, label)))

    @property
    def cancelled(self):
        return self.cancel.is_set()


class Worker:
    """Одна фоновая задача за раз; общение с окном через очередь."""

    def __init__(self, app):
        self.app = app
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.thread = None
        self.app.after(80, self._pump)

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, fn, on_done=None, name="работа"):
        if self.busy:
            messagebox.showinfo("Занято", "Дождитесь окончания текущей операции\n"
                                          "или нажмите «Стоп».")
            return False
        self.cancel.clear()
        ctx = Ctx(self.q, self.cancel)

        def run():
            try:
                res = fn(ctx)
                self.q.put(("done", (on_done, res)))
            except Exception as e:
                self.q.put(("log", traceback.format_exc()))
                self.q.put(("error", f"{name}: {e}"))
                self.q.put(("done", (on_done, None)))

        # Сообщение ставим и перерисовываем ДО старта потока: дальше поток
        # займёт процессор, и окно какое-то время не будет обновляться.
        self.app.set_busy(True, name)
        try:
            self.app.update_idletasks()
        except Exception:
            pass
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.cancel.set()

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.app.log(payload)
                elif kind == "status":
                    self.app.status(payload)
                elif kind == "busy":
                    self.app.set_indeterminate(payload)
                elif kind == "progress":
                    self.app.set_progress(*payload)
                elif kind == "error":
                    self.app.log("ОШИБКА: " + payload)
                    self.app.status("ошибка — см. журнал")
                    messagebox.showerror("Ошибка", payload)
                elif kind == "done":
                    cb, res = payload
                    self.app.set_busy(False)
                    if cb:
                        try:
                            cb(res)
                        except Exception:
                            self.app.log(traceback.format_exc())
        except queue.Empty:
            pass
        self.app.after(80, self._pump)


# ---------------------------------------------------------------- диалог ударения

class StressDialog(tk.Toplevel):
    """Как читать это слово: ударение и/или «ё», и насколько широко применить.

    Три области действия, потому что ошибки бывают трёх сортов:
      «только здесь»   -- ударение зависит от места (старый замок / висит замок);
      «в этой главе»   -- одно и то же слово во всей главе;
      «в словарь»      -- слово всегда читается так, во всех книгах.
    """

    def __init__(self, parent, word: str, context: str = ""):
        super().__init__(parent)
        self.title("Как читать слово")
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        plain = word.replace("+", "")
        variants = core.stress_variants(plain)

        ttk.Label(self, text=plain, font=("Segoe UI", 14, "bold")
                  ).grid(row=0, column=0, padx=16, pady=(14, 2), sticky="w")
        if context:
            ttk.Label(self, text=context, style="Hint.TLabel",
                      wraplength=parent.px(460)
                      ).grid(row=1, column=0, padx=16, sticky="w")

        self.var = tk.StringVar(value=word if ("+" in word or "ё" in word.lower())
                                else (variants[0][0] if variants else plain))
        box = ttk.LabelFrame(self, text="вариант чтения")
        box.grid(row=2, column=0, padx=16, pady=10, sticky="we")
        for i, (form, note) in enumerate(variants):
            ttk.Radiobutton(box, text=f"  {form}", value=form, variable=self.var,
                            style="Big.TRadiobutton").grid(row=i, column=0, sticky="w",
                                                           padx=6, pady=1)
            ttk.Label(box, text=note, style="Hint.TLabel").grid(row=i, column=1,
                                                                sticky="w", padx=10)
        if not variants:
            ttk.Label(box, text="в слове нет гласных").grid(row=0, column=0, padx=6)

        self.scope = tk.StringVar(value="here")
        sc = ttk.LabelFrame(self, text="где применить")
        sc.grid(row=3, column=0, padx=16, sticky="we")
        for val, txt in (("here", "только здесь (запомнится как правка этого абзаца)"),
                         ("file", "везде в этой главе"),
                         ("dict", "везде и во всех главах — в словарь")):
            ttk.Radiobutton(sc, text=txt, value=val, variable=self.scope
                            ).pack(anchor="w", padx=6, pady=1)

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, padx=16, pady=12, sticky="e")
        ttk.Button(btns, text="Применить", command=self._ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Прослушать", command=self._listen).pack(side="left", padx=4)
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="left")
        self.parent = parent
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_visibility()
        self.focus()

    def _listen(self):
        self.parent.speak_text(self.var.get())

    def _ok(self):
        self.result = (self.var.get(), self.scope.get())
        self.destroy()


# ---------------------------------------------------------------- главное окно

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Аудиокнига — ударения и озвучка")
        # Размеры экранных элементов задаются в пикселях, а шрифты -- в пунктах,
        # и на мониторе с масштабированием (4K, 240 dpi) пиксельные размеры надо
        # растягивать вместе со шрифтом, иначе в окно влезает пара абзацев.
        self.k = max(1.0, self.winfo_fpixels("1i") / 96.0)
        w = min(self.px(1180), int(self.winfo_screenwidth() * 0.9))
        h = min(self.px(780), int(self.winfo_screenheight() * 0.88))
        self.geometry(f"{w}x{h}+{self.px(30)}+{self.px(20)}")
        self.minsize(min(self.px(760), w), min(self.px(520), h))

        self.settings = self._load_settings()
        self.dictionary = core.load_dictionary(DICT_PATH)
        core.load_yo_list(YO_LIST_PATH)
        self.files = list(self.settings.get("files", []))
        self.current_file = None
        self.acc_engine = None          # core.Accentizer (живёт между задачами)
        self.synth = None               # core.Synth
        self.worker = Worker(self)
        self._tmp_wav = os.path.join(tempfile.gettempdir(), "audiobook_preview.wav")
        self._prog_mode = "determinate"
        self._busy_buttons = []       # кнопки, которые гаснут на время работы
        self._overlay = None          # табличка «идёт загрузка»

        self._style()
        self._build()
        self._refresh_voices()
        self._refresh_files()
        self.protocol("WM_DELETE_WINDOW", self._quit)
        # разделители панелей ставим после первой отрисовки: до неё у окна
        # ещё нет реального размера, и вес панелей не применяется
        self.after(120, self._init_sashes)

    def _init_sashes(self):
        try:
            self.update_idletasks()
            self.pan.sashpos(0, int(self.winfo_width() * 0.30))
            self.rpan.sashpos(0, int(self.winfo_height() * 0.62))
        except Exception:
            pass

    # -- оформление ----------------------------------------------------
    def px(self, n):
        """Пиксели с учётом масштаба экрана."""
        return int(n * self.k)

    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        st.configure("Big.TRadiobutton", font=("Segoe UI", 12))
        st.configure("Head.TLabel", font=("Segoe UI", 11, "bold"))
        st.configure("Hint.TLabel", foreground="#666")
        # высота строки таблицы задаётся в пикселях и на 4K-экране обрезает текст
        st.configure("Treeview", rowheight=self.px(24))

    def _menu(self):
        bar = tk.Menu(self)
        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label="О программе", command=self.show_about)
        help_menu.add_command(label="Руководство (README)", command=self.open_readme)
        help_menu.add_separator()
        help_menu.add_command(label="Открыть папку приложения",
                              command=lambda: os.startfile(APP_DIR))
        bar.add_cascade(label="Справка", menu=help_menu)
        self.configure(menu=bar)

    def open_readme(self):
        path = os.path.join(APP_DIR, "README.md")
        if os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo("README", "Файл README.md рядом с программой не найден.")

    def about_text(self):
        """Сведения о программе — они же полезны в отчёте об ошибке."""
        model = self.model_var.get() if hasattr(self, "model_var") else core.DEFAULT_MODEL
        nc = " (некоммерческая лицензия!)" if model != "v5_cis_base" else ""
        return "\n".join([
            f"Аудиокнига {core.VERSION}",
            "",
            "Расстановка ударений и озвучка текстовых глав.",
            "Работает офлайн: RUAccent ставит ударения, Silero читает,",
            "ffmpeg собирает mp3 и выравнивает громкость.",
            "",
            "Идея, постановка задач и приёмка на слух — dbacchus.",
            "Код — Claude (Anthropic) под его руководством.",
            "",
            "Код приложения: лицензия MIT.",
            "Модель v5_cis_base — MIT; русские модели Silero (v5_5_ru и др.) —",
            "CC BY-NC, только некоммерческое использование.",
            "RUAccent — Apache 2.0, PyTorch — BSD, ONNX Runtime — MIT,",
            "словарь произношений CMUdict (Carnegie Mellon) — BSD,",
            "ffmpeg — LGPL-сборка, скачивается отдельно.",
            "",
            "— — —",
            f"Python {sys.version.split()[0]}, "
            f"{'собранная версия' if getattr(sys, 'frozen', False) else 'запуск из исходников'}",
            f"модель: {model}{nc}",
            f"папка: {APP_DIR}",
            f"файл модели: {core.find_model_file(model) or 'не найден'}",
            f"модели ударений: {core.ruaccent_workdir() or 'из пакета ruaccent'}",
            f"ffmpeg: {core.ffmpeg_exe() or 'не найден'}",
        ])

    def show_about(self):
        win = tk.Toplevel(self)
        win.title("О программе")
        win.transient(self)
        win.resizable(False, False)
        text = self.about_text()
        head, _, tech = text.partition("— — —")

        tk.Label(win, text=f"Аудиокнига {core.VERSION}", font=("Segoe UI", 16, "bold")
                 ).pack(padx=self.px(24), pady=(self.px(18), 0), anchor="w")
        tk.Label(win, text=head.split("\n", 2)[2].strip(), justify="left",
                 font=("Segoe UI", 10)).pack(padx=self.px(24), pady=self.px(8), anchor="w")
        tk.Label(win, text=tech.strip(), justify="left", font=("Consolas", 8),
                 fg="#555").pack(padx=self.px(24), pady=(0, self.px(8)), anchor="w")

        btns = ttk.Frame(win)
        btns.pack(padx=self.px(24), pady=(0, self.px(16)), anchor="e")
        ttk.Button(btns, text="Скопировать сведения",
                   command=lambda: (self.clipboard_clear(), self.clipboard_append(text),
                                    self.status("Сведения скопированы"))).pack(side="left", padx=4)
        ttk.Button(btns, text="Закрыть", command=win.destroy).pack(side="left")
        win.bind("<Escape>", lambda e: win.destroy())
        win.grab_set()

    def _build(self):
        self._menu()
        # Нижнюю панель закрепляем ПЕРВОЙ и снизу: если её паковать после
        # вкладок, места ей не остаётся и она уезжает за край окна вместе с
        # состоянием и полосой прогресса (так и было: их не было видно).
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=10, pady=6)
        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(bar, textvariable=self.status_var, style="Head.TLabel"
                  ).pack(side="left")
        self.prog = ttk.Progressbar(bar, length=self.px(260), mode="determinate")
        self.prog.pack(side="right")
        self.btn_stop = ttk.Button(bar, text="Стоп", width=8, command=self.worker.stop,
                                   state="disabled")
        self.btn_stop.pack(side="right", padx=8)

        nb = ttk.Notebook(self)
        nb.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 0))
        self.nb = nb
        nb.add(self._tab_text(nb), text="  1. Текст и ударения  ")
        nb.add(self._tab_voice(nb), text="  2. Озвучка  ")
        nb.add(self._tab_dict(nb), text="  3. Словарь  ")

    # -- вкладка 1: текст ----------------------------------------------
    def _tab_text(self, parent):
        f = ttk.Frame(parent)
        pan = ttk.PanedWindow(f, orient="horizontal")
        pan.pack(fill="both", expand=True, padx=6, pady=6)

        self.pan = pan
        left = ttk.Frame(pan)
        pan.add(left, weight=2)
        ttk.Label(left, text="Главы (файлы .txt)", style="Head.TLabel").pack(anchor="w")
        cols = ("acc", "edits", "dur")
        self.tree = ttk.Treeview(left, columns=cols, selectmode="extended", height=14)
        self.tree.heading("#0", text="Файл")
        self.tree.heading("acc", text="Ударения")
        self.tree.heading("edits", text="Правки")
        self.tree.heading("dur", text="~мин")
        self.tree.column("#0", width=self.px(170))
        self.tree.column("acc", width=self.px(70), anchor="center")
        self.tree.column("edits", width=self.px(55), anchor="center")
        self.tree.column("dur", width=self.px(50), anchor="e")
        self.tree.pack(fill="both", expand=True, pady=(2, 4))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._open_selected())

        b1 = ttk.Frame(left)
        b1.pack(fill="x")
        ttk.Button(b1, text="Добавить…", command=self.add_files).pack(side="left")
        ttk.Button(b1, text="Папку…", command=self.add_folder).pack(side="left", padx=3)
        ttk.Button(b1, text="Убрать", command=self.remove_file).pack(side="left")
        ttk.Button(b1, text="↑", width=3, command=lambda: self.move_file(-1)).pack(side="left", padx=(8, 0))
        ttk.Button(b1, text="↓", width=3, command=lambda: self.move_file(1)).pack(side="left")

        b2 = ttk.Frame(left)
        b2.pack(fill="x", pady=6)
        self.btn_accent = ttk.Button(b2, text="Расставить ударения",
                                     command=lambda: self.run_accent(False))
        self.btn_accent.pack(side="left")
        self.btn_accent_all = ttk.Button(b2, text="во всех главах",
                                         command=lambda: self.run_accent(True))
        self.btn_accent_all.pack(side="left", padx=4)
        self._busy_buttons += [self.btn_accent, self.btn_accent_all]

        b4 = ttk.Frame(left)
        b4.pack(fill="x", pady=(2, 0))
        ttk.Button(b4, text="Выгрузить спорные…",
                   command=self.export_check).pack(side="left")
        ttk.Button(b4, text="Импорт проверенного…",
                   command=self.import_check).pack(side="left", padx=4)
        ttk.Button(left, text="Забыть ручные правки этой главы",
                   command=self.forget_edits).pack(anchor="w", pady=(4, 0))

        rpan = ttk.PanedWindow(pan, orient="vertical")
        pan.add(rpan, weight=5)
        self.rpan = rpan
        right = ttk.Frame(rpan)
        rpan.add(right, weight=3)
        head = ttk.Frame(right)
        head.pack(fill="x")
        self.file_label = tk.StringVar(value="файл не выбран")
        ttk.Label(head, textvariable=self.file_label, style="Head.TLabel").pack(side="left")
        self.view_var = tk.StringVar(value="acc")
        ttk.Radiobutton(head, text="с ударениями", value="acc", variable=self.view_var,
                        command=self._reload_view).pack(side="right")
        ttk.Radiobutton(head, text="исходный", value="src", variable=self.view_var,
                        command=self._reload_view).pack(side="right", padx=6)

        self.font_size = self.settings.get("font_size", 12)
        self.text = tk.Text(right, wrap="word", font=("Segoe UI", self.font_size),
                            undo=True, spacing1=self.px(4), spacing3=self.px(6),
                            padx=self.px(10), pady=self.px(8))
        # Ctrl+колесо -- размер шрифта редактора
        self.text.bind("<Control-MouseWheel>",
                       lambda e: self.set_font_size(self.font_size + (1 if e.delta > 0 else -1)))
        sb = ttk.Scrollbar(right, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(fill="both", expand=True, pady=4)
        self.text.tag_configure("edited", background="#f2f7ff")
        self.text.tag_configure("yo", background="#e6dbff")
        self.text.tag_configure("homograph", background="#fff3b0")
        self.text.tag_configure("nostress", background="#ffe0e0")
        self.text.tag_configure("indict", background="#d9f2d9")
        self.text.tag_configure("found", background="#8ab4f8")
        self.text.bind("<Double-1>", self._word_click)

        b3 = ttk.Frame(right)
        b3.pack(fill="x", pady=(0, 2))
        ttk.Button(b3, text="Сохранить", command=self.save_text).pack(side="left")
        ttk.Button(b3, text="Прослушать выделенное",
                   command=self.preview_selection).pack(side="left", padx=6)
        ttk.Button(b3, text="Проверить", command=self.check_text).pack(side="left")
        ttk.Button(b3, text="Открыть в редакторе",
                   command=self.open_external).pack(side="left", padx=6)
        ttk.Label(b3, text="двойной клик по слову — поставить ударение или ё;   "
                           "сиреневое — е/ё, жёлтое — омограф, розовое — без "
                           "ударения, зелёное — из словаря",
                  style="Hint.TLabel").pack(side="left", padx=10)

        # ---- нижняя панель: места для проверки, с контекстом ----
        low = ttk.Frame(rpan)
        rpan.add(low, weight=1)
        fl = ttk.Frame(low)
        fl.pack(fill="x")
        ttk.Label(fl, text="Проверить глазами:", style="Head.TLabel").pack(side="left")
        self.f_yo = tk.BooleanVar(value=True)
        self.f_hom = tk.BooleanVar(value=True)
        self.f_no = tk.BooleanVar(value=False)
        for text_, var in (("е / ё", self.f_yo), ("омографы", self.f_hom),
                           ("без ударения", self.f_no)):
            ttk.Checkbutton(fl, text=text_, variable=var,
                            command=self.check_text).pack(side="left", padx=8)
        self.issue_count = tk.StringVar(value="")
        ttk.Label(fl, textvariable=self.issue_count, style="Hint.TLabel").pack(side="left", padx=10)
        ttk.Label(fl, text="двойной клик — правка", style="Hint.TLabel").pack(side="right")

        wrap = ttk.Frame(low)
        wrap.pack(fill="both", expand=True, pady=2)
        self.issues = ttk.Treeview(wrap, columns=("word", "kind", "context"),
                                   show="headings", height=7)
        self.issues.heading("word", text="Слово")
        self.issues.heading("kind", text="Что проверить")
        self.issues.heading("context", text="Контекст")
        self.issues.column("word", width=self.px(130), stretch=False)
        self.issues.column("kind", width=self.px(150), stretch=False)
        self.issues.column("context", width=self.px(700))
        isb = ttk.Scrollbar(wrap, command=self.issues.yview)
        self.issues.configure(yscrollcommand=isb.set)
        isb.pack(side="right", fill="y")
        self.issues.pack(side="left", fill="both", expand=True)
        self.issues.bind("<<TreeviewSelect>>", self._goto_issue)
        self.issues.bind("<Double-1>", self._edit_issue)
        self._issue_pos = {}
        return f

    # -- вкладка 2: озвучка --------------------------------------------
    def _tab_voice(self, parent):
        f = ttk.Frame(parent)
        s = self.settings
        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=12, pady=10)

        r = 0
        ttk.Label(grid, text="Голос", style="Head.TLabel").grid(row=r, column=0, sticky="w")
        self.voice_var = tk.StringVar(value=s.get("voice", ""))
        vb = ttk.Combobox(grid, width=30, state="readonly")
        vb.grid(row=r, column=1, sticky="w", padx=6)
        vb.bind("<<ComboboxSelected>>",
                lambda e: self.voice_var.set(self._voice_by_title.get(vb.get(), "")))
        self.voice_box = vb
        self.btn_sample = ttk.Button(grid, text="Образец голоса",
                                     command=self.preview_voice)
        self.btn_sample.grid(row=r, column=2, padx=8, sticky="w")

        r += 1
        ttk.Label(grid, text="Модель").grid(row=r, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar(value=s.get("model", core.DEFAULT_MODEL))
        mb = ttk.Combobox(grid, textvariable=self.model_var, width=30, state="readonly",
                          values=core.MODELS)
        mb.grid(row=r, column=1, sticky="w", padx=6)
        mb.bind("<<ComboboxSelected>>", lambda e: self._refresh_voices())
        ttk.Label(grid, text="v5_cis_base — 29 голосов, свободная лицензия;\n"
                             "v5_5_ru — 5 голосов, только некоммерческое использование",
                  style="Hint.TLabel", justify="left"
                  ).grid(row=r, column=2, sticky="w", padx=8)

        r += 1
        ttk.Label(grid, text="Темп").grid(row=r, column=0, sticky="w", pady=4)
        self.rate_var = tk.StringVar(value=s.get("rate", "обычный"))
        ttk.Combobox(grid, textvariable=self.rate_var, width=26, state="readonly",
                     values=["обычный", "медленно", "быстро"]
                     ).grid(row=r, column=1, sticky="w", padx=6)

        r += 1
        ttk.Label(grid, text="Потоков CPU").grid(row=r, column=0, sticky="w", pady=4)
        self.threads_var = tk.IntVar(value=s.get("threads", min(6, os.cpu_count() or 4)))
        ttk.Spinbox(grid, from_=1, to=(os.cpu_count() or 8), width=6,
                    textvariable=self.threads_var).grid(row=r, column=1, sticky="w", padx=6)

        r += 1
        ttk.Label(grid, text="Паузы, с").grid(row=r, column=0, sticky="w", pady=4)
        pf = ttk.Frame(grid)
        pf.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        self.p_par = tk.DoubleVar(value=s.get("pause_par", 0.4))
        self.p_title = tk.DoubleVar(value=s.get("pause_title", 1.4))
        self.p_brk = tk.DoubleVar(value=s.get("pause_break", 1.2))
        for label, var in (("абзац", self.p_par), ("заголовок", self.p_title),
                           ("сцена ***", self.p_brk)):
            ttk.Label(pf, text=label).pack(side="left")
            ttk.Spinbox(pf, from_=0, to=5, increment=0.1, width=5, textvariable=var
                        ).pack(side="left", padx=(3, 12))

        r += 1
        opts = ttk.Frame(grid)
        opts.grid(row=r, column=0, columnspan=3, sticky="w", pady=8)
        self.ssml_var = tk.BooleanVar(value=s.get("ssml", True))
        self.loud_var = tk.BooleanVar(value=s.get("loudnorm", True))
        self.merge_var = tk.BooleanVar(value=s.get("merge", True))
        self.useacc_var = tk.BooleanVar(value=s.get("use_acc", True))
        self.split_var = tk.BooleanVar(value=s.get("split_chapters", False))
        ttk.Checkbutton(opts, text="SSML (паузы, разгон фрагмента)",
                        variable=self.ssml_var).pack(side="left")
        ttk.Checkbutton(opts, text="выровнять громкость",
                        variable=self.loud_var).pack(side="left", padx=12)
        ttk.Checkbutton(opts, text="склеить всё в один файл",
                        variable=self.merge_var).pack(side="left")
        ttk.Checkbutton(opts, text="читать файлы с ударениями",
                        variable=self.useacc_var).pack(side="left", padx=12)
        ttk.Checkbutton(opts, text="делить по заголовкам «# Глава…»",
                        variable=self.split_var).pack(side="left")

        r += 1
        ttk.Label(grid, text="Книга / автор").grid(row=r, column=0, sticky="w")
        mf = ttk.Frame(grid)
        mf.grid(row=r, column=1, columnspan=2, sticky="we", padx=6)
        self.album_var = tk.StringVar(value=s.get("album", ""))
        self.artist_var = tk.StringVar(value=s.get("artist", ""))
        ttk.Entry(mf, textvariable=self.album_var, width=28).pack(side="left")
        ttk.Entry(mf, textvariable=self.artist_var, width=24).pack(side="left", padx=6)

        r += 1
        ttk.Label(grid, text="Папка вывода").grid(row=r, column=0, sticky="w", pady=6)
        of = ttk.Frame(grid)
        of.grid(row=r, column=1, columnspan=2, sticky="we", padx=6)
        self.out_var = tk.StringVar(value=s.get("out_dir", os.path.join(APP_DIR, "audio")))
        ttk.Entry(of, textvariable=self.out_var, width=52).pack(side="left")
        ttk.Button(of, text="…", width=3, command=self.pick_out).pack(side="left", padx=4)
        ttk.Button(of, text="Открыть", command=self.open_out).pack(side="left")

        act = ttk.Frame(f)
        act.pack(fill="x", padx=12, pady=4)
        b_sel = ttk.Button(act, text="Озвучить выбранные главы",
                           command=lambda: self.run_render(False))
        b_sel.pack(side="left")
        b_all = ttk.Button(act, text="Озвучить все",
                           command=lambda: self.run_render(True))
        b_all.pack(side="left", padx=8)
        self._busy_buttons += [b_sel, b_all, self.btn_sample]

        ttk.Label(f, text="Журнал", style="Head.TLabel").pack(anchor="w", padx=12, pady=(8, 0))
        self.logbox = tk.Text(f, height=12, font=("Consolas", 9), bg="#1e1e1e",
                              fg="#d4d4d4", insertbackground="#d4d4d4")
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        return f

    def _refresh_voices(self):
        """Список голосов зависит от модели: у v5_cis_base их 29, у v5_5_ru пять."""
        voices = core.voices_for(self.model_var.get())
        self._voice_by_title = {v: k for k, v in voices.items()}
        titles = list(voices.values())
        self.voice_box.configure(values=titles)
        current = self.voice_var.get()
        if current in voices:
            self.voice_box.set(voices[current])
        else:                               # голос от другой модели -- берём первый
            self.voice_box.set(titles[0])
            self.voice_var.set(self._voice_by_title[titles[0]])

    # -- вкладка 3: словарь --------------------------------------------
    def _tab_dict(self, parent):
        f = ttk.Frame(parent)
        ttk.Label(f, text="Словарь ударений: применяется поверх автомата и всегда "
                          "побеждает. «+» ставится ПЕРЕД ударной гласной.",
                  style="Hint.TLabel").pack(anchor="w", padx=12, pady=(10, 4))
        body = ttk.Frame(f)
        body.pack(fill="both", expand=True, padx=12, pady=4)
        self.dict_tree = ttk.Treeview(body, columns=("word", "acc"), show="headings")
        self.dict_tree.heading("word", text="Слово")
        self.dict_tree.heading("acc", text="С ударением")
        self.dict_tree.column("word", width=self.px(220))
        self.dict_tree.column("acc", width=self.px(260))
        sb = ttk.Scrollbar(body, command=self.dict_tree.yview)
        self.dict_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.dict_tree.pack(side="left", fill="both", expand=True)

        add = ttk.Frame(f)
        add.pack(fill="x", padx=12, pady=6)
        self.dw = tk.StringVar()
        self.da = tk.StringVar()
        ttk.Label(add, text="слово").pack(side="left")
        ttk.Entry(add, textvariable=self.dw, width=20).pack(side="left", padx=4)
        ttk.Label(add, text="с ударением").pack(side="left", padx=(8, 0))
        ttk.Entry(add, textvariable=self.da, width=22).pack(side="left", padx=4)
        ttk.Button(add, text="Добавить", command=self.dict_add).pack(side="left", padx=6)
        ttk.Button(add, text="Удалить выбранное", command=self.dict_del).pack(side="left")
        ttk.Button(add, text="Загрузить…", command=self.dict_load).pack(side="right")
        ttk.Button(add, text="Сохранить как…", command=self.dict_export).pack(side="right", padx=6)
        self._refresh_dict()
        return f

    # -- состояние окна ------------------------------------------------
    def log(self, text):
        self.logbox.insert("end", text.rstrip() + "\n")
        self.logbox.see("end")

    def status(self, text):
        self.status_var.set(text)

    def show_overlay(self, text):
        """Табличка поверх окна на время загрузки моделей.

        Анимацию здесь показать нельзя: модели грузятся долгими вызовами
        внутри библиотек, которые не отпускают интерпретатор, и окно
        замирает на секунду-полторы (измерено). Бегущая полоса в такие
        паузы просто стоит. Поэтому сигнал статический: крупная надпись,
        отрисованная ДО начала загрузки, и честное предупреждение, что
        окно на это время подвиснет."""
        if self._overlay is None:
            self._overlay = tk.Frame(self, bg="#fff6cc", bd=1, relief="solid")
            self._overlay_label = tk.Label(
                self._overlay, bg="#fff6cc", fg="#222", justify="center",
                font=("Segoe UI", 13), padx=self.px(28), pady=self.px(18))
            self._overlay_label.pack()
        self._overlay_label.configure(text=text)
        self._overlay.place(relx=0.5, rely=0.42, anchor="center")
        self._overlay.lift()
        self.update_idletasks()          # отрисовать прямо сейчас

    def hide_overlay(self):
        if self._overlay is not None:
            self._overlay.place_forget()

    def set_indeterminate(self, text=""):
        """Бегущая полоса: работа идёт, но сколько её -- заранее неизвестно."""
        if self._prog_mode != "indeterminate":
            self._prog_mode = "indeterminate"
            self.prog.configure(mode="indeterminate")
            self.prog.start(14)
        if text:
            self.status(text)

    def set_progress(self, cur, total, label=""):
        self.hide_overlay()              # пошла считаемая работа
        if self._prog_mode != "determinate":
            self.prog.stop()
            self.prog.configure(mode="determinate")
            self._prog_mode = "determinate"
            self._t_start = time.time()
        self.prog["maximum"] = max(1, total)
        self.prog["value"] = cur
        # оценка остатка: первые проценты врут, поэтому ждём десятую долю
        left = ""
        if cur >= max(3, total // 10) and cur < total:
            spent = time.time() - getattr(self, "_t_start", time.time())
            rest = spent / cur * (total - cur)
            left = f", осталось ~{rest/60:.0f} мин" if rest > 90 else f", осталось ~{rest:.0f} с"
        pct = 100 * cur // max(1, total)
        self.status_var.set(f"{pct}%  ({cur} из {total}{left})  {label[:50]}")

    def set_busy(self, busy, name=""):
        self.btn_stop.configure(state="normal" if busy else "disabled")
        for b in self._busy_buttons:
            try:
                b.configure(state="disabled" if busy else "normal")
            except Exception:
                pass
        try:
            self.configure(cursor="watch" if busy else "")
        except Exception:
            pass
        if busy:
            # если перед запуском уже сказали что-то по делу («Загружаю
            # модели…»), не затираем это общим названием операции
            if self.status_var.get() in ("", "Готово"):
                self.status(f"{name}…")
        else:
            self.hide_overlay()
            self.prog.stop()
            self.prog.configure(mode="determinate")
            self._prog_mode = "determinate"
            self.status("Готово")
            self.prog["value"] = 0

    # -- список файлов -------------------------------------------------
    def _refresh_files(self):
        self.tree.delete(*self.tree.get_children())
        for p in self.files:
            name = os.path.basename(p)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            accp = self._acc_path(p)
            acc = "есть" if os.path.exists(accp) else "—"
            n_ed = len(core.load_edits(accp))
            # оценка длительности по размеру файла: на «Пороге» 111 858 байт
            # UTF-8 дали 83.7 минуты звука, то есть примерно байт/1350 = минуты
            self.tree.insert("", "end", iid=p, text=name,
                             values=(acc, n_ed or "—", f"{size/1350:.0f}"))

    def _acc_path(self, p):
        base, ext = os.path.splitext(p)
        new = (base if ext.lower() in (".txt", ".md") else p) + ACC_SUFFIX
        old = p + ACC_SUFFIX          # как называлось раньше для .md
        if new != old and not os.path.exists(new) and os.path.exists(old):
            return old                # уже сделанный файл не бросаем
        return new

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Выберите текстовые файлы (главы)",
            filetypes=[("Текст", "*.txt *.md"), ("Все файлы", "*.*")],
            initialdir=self.settings.get("last_dir", APP_DIR))
        for p in paths:
            if p.endswith(ACC_SUFFIX):
                continue
            if p not in self.files:
                self.files.append(p)
        if paths:
            self.settings["last_dir"] = os.path.dirname(paths[0])
        self._refresh_files()

    def add_folder(self):
        d = filedialog.askdirectory(title="Папка с главами",
                                    initialdir=self.settings.get("last_dir", APP_DIR))
        if not d:
            return
        found = sorted(os.path.join(d, n) for n in os.listdir(d)
                       if n.lower().endswith((".txt", ".md")) and not n.endswith(ACC_SUFFIX))
        for p in found:
            if p not in self.files:
                self.files.append(p)
        self.settings["last_dir"] = d
        self._refresh_files()
        self.log(f"Добавлено файлов: {len(found)}")

    def remove_file(self):
        for p in self.tree.selection():
            if p in self.files:
                self.files.remove(p)
        self._refresh_files()

    def move_file(self, d):
        sel = self.tree.selection()
        if not sel:
            return
        p = sel[0]
        i = self.files.index(p)
        j = max(0, min(len(self.files) - 1, i + d))
        self.files.insert(j, self.files.pop(i))
        self._refresh_files()
        self.tree.selection_set(p)

    def selected_files(self, all_files=False):
        if all_files:
            return list(self.files)
        sel = [p for p in self.tree.selection()]
        return sel or list(self.files)

    # -- редактор ------------------------------------------------------
    def _open_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        p = sel[0]
        if p == self.current_file:
            return
        if self._dirty():
            if messagebox.askyesno("Сохранить?", "Текст изменён. Сохранить?"):
                self.save_text()
        self.current_file = p
        self._reload_view()

    def _reload_view(self):
        p = self.current_file
        if not p:
            return
        accp = self._acc_path(p)
        want_acc = self.view_var.get() == "acc"
        path = accp if (want_acc and os.path.exists(accp)) else p
        note = "   (ударения ещё не расставлены)" if want_acc and path == p else ""
        self.file_label.set(os.path.basename(path) + note)
        try:
            with open(path, encoding="utf-8") as f:
                data = f.read()
            with open(p, encoding="utf-8") as f:
                self._src_text = f.read()
        except Exception as e:
            self.log(f"Не открыть {path}: {e}")
            return
        self._loaded_path = path
        self._loaded_text = data
        self._edits = core.load_edits(accp) if path == accp else {}
        self.text.delete("1.0", "end")
        self.text.insert("1.0", data)
        self.text.edit_modified(False)
        self.highlight()
        self.check_text()

    def _dirty(self):
        return bool(self.text.edit_modified()) and getattr(self, "_loaded_path", None)

    def save_text(self):
        """Сохранение. Для файла с ударениями заодно запоминаем, какие строки
        поправлены руками: контекстные случаи («старый замок» / «висит замок»)
        словарём не лечатся, и такая правка должна пережить повторную
        расстановку ударений. Ключ правки — сама строка исходника."""
        path = getattr(self, "_loaded_path", None)
        if not path:
            return
        data = self.text.get("1.0", "end-1c")
        with open(path, "w", encoding="utf-8") as f:
            f.write(data)
        msg = f"Сохранено: {os.path.basename(path)}"
        if self.current_file and path == self._acc_path(self.current_file):
            edits, n = core.collect_edits(self._src_text, self._loaded_text, data,
                                          dict(getattr(self, "_edits", {})))
            if n < 0:
                self.log("Строк стало больше или меньше, чем в исходнике: правки "
                         "этого сохранения не привязаны к абзацам и пропадут при "
                         "повторной расстановке ударений.")
            elif n:
                core.save_edits(path, edits)
                self._edits = edits
                msg += f"; запомнено правок: {n} (всего {len(edits)})"
        self._loaded_text = data
        self.text.edit_modified(False)
        self.log(msg)
        self._refresh_files()

    def forget_edits(self):
        p = self.current_file
        if not p:
            return
        accp = self._acc_path(p)
        n = len(core.load_edits(accp))
        if not n:
            self.log("В этой главе ручных правок нет.")
            return
        if messagebox.askyesno("Правки", f"Забыть {n} ручных правок этой главы?\n"
                                         "Текст останется как есть, но при следующей "
                                         "расстановке ударений эти места прочитает автомат."):
            core.save_edits(accp, {})
            self._edits = {}
            self.log("Ручные правки главы забыты.")
            self._refresh_files()
            self.highlight()

    def open_external(self):
        path = getattr(self, "_loaded_path", None)
        if path and os.path.exists(path):
            os.startfile(path)

    def export_check(self):
        """Спорные строки главы -> отдельный файл. Его можно просмотреть в любом
        редакторе (или отдать на проверку) и вернуть кнопкой «Импорт»."""
        p = self.current_file
        if not p:
            return
        accp = self._acc_path(p)
        if not os.path.exists(accp):
            messagebox.showinfo("Ударения", "Сначала расставьте ударения в этой главе.")
            return
        if self._dirty():
            self.save_text()
        kinds = tuple(k for k, v in (("yo", self.f_yo), ("homograph", self.f_hom),
                                     ("nostress", self.f_no)) if v.get()) or ("yo", "homograph")
        with open(accp, encoding="utf-8") as f:
            data = f.read()
        out = core.export_check(data, os.path.basename(p), kinds)
        dst = os.path.splitext(p)[0] + ".check.txt"
        with open(dst, "w", encoding="utf-8") as f:
            f.write(out)
        self.log(f"Спорные места: {dst}  (строк: {out.count('] (')})")
        os.startfile(dst)

    def import_check(self):
        """Забрать проверенный файл обратно: строки ложатся в главу и
        запоминаются как ручные правки."""
        p = self.current_file
        if not p:
            return
        accp = self._acc_path(p)
        guess = os.path.splitext(p)[0] + ".check.txt"
        src = filedialog.askopenfilename(
            title="Проверенный файл", filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")],
            initialdir=os.path.dirname(p),
            initialfile=os.path.basename(guess) if os.path.exists(guess) else "")
        if not src:
            return
        with open(src, encoding="utf-8") as f:
            fixes = core.parse_check(f.read())
        with open(accp, encoding="utf-8") as f:
            data = f.read()
        new, n = core.apply_check(data, fixes)
        if not n:
            self.log("Импорт: изменений нет.")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", new)
        self.text.edit_modified(True)
        self.save_text()                      # правки попадут в список правок главы
        self.highlight()
        self.check_text()
        self.log(f"Импорт: изменено строк {n} из {len(fixes)} проверенных.")

    def highlight(self):
        """Цвет = повод посмотреть: сиреневый — выбор е/ё, жёлтый — омограф,
        розовый — слово осталось без ударения, зелёный — слово из словаря.
        Строки, поправленные руками, помечены светлым фоном целиком."""
        for tag in ("yo", "homograph", "nostress", "indict", "found", "edited"):
            self.text.tag_remove(tag, "1.0", "end")
        data = self.text.get("1.0", "end-1c")
        edits = getattr(self, "_edits", {}) or {}
        src_lines = getattr(self, "_src_text", "").splitlines()
        for i, line in enumerate(data.splitlines(), 1):
            if edits and line.strip() and i <= len(src_lines):
                if core.line_key(src_lines[i - 1]) in edits:
                    self.text.tag_add("edited", f"{i}.0", f"{i}.end")
            for m in core.WORD_RE.finditer(line):
                w = m.group(0)
                plain = w.replace("+", "").lower()
                if not plain:
                    continue
                a, b = f"{i}.{m.start()}", f"{i}.{m.end()}"
                if plain in self.dictionary or plain.replace("ё", "е") in self.dictionary:
                    self.text.tag_add("indict", a, b)
                    continue
                kind = core.classify_word(w)
                if kind:
                    self.text.tag_add(kind, a, b)

    def check_text(self):
        """Список мест для проверки: слово, что проверяем и фраза целиком —
        чтобы «все/всё» просматривать списком фраз, а не глазами по главе."""
        kinds = {k for k, v in (("yo", self.f_yo), ("homograph", self.f_hom),
                                ("nostress", self.f_no)) if v.get()}
        self.issues.delete(*self.issues.get_children())
        self._issue_pos = {}
        if not kinds:
            self.issue_count.set("")
            return
        data = self.text.get("1.0", "end-1c")
        found = core.analyze(data, kinds)
        label = {"yo": "е или ё — по смыслу", "homograph": "ударение — по смыслу",
                 "nostress": "нет ударения"}
        limit = 800
        for i, it in enumerate(found[:limit]):
            iid = f"i{i}"
            self._issue_pos[iid] = (it["line"], it["col"], it["word"])
            self.issues.insert("", "end", iid=iid,
                               values=(it["word"], label[it["kind"]], it["context"]))
        n = {k: sum(1 for it in found if it["kind"] == k) for k in label}
        self.issue_count.set(f"е/ё: {n['yo']}   омографов: {n['homograph']}   "
                             f"без ударения: {n['nostress']}"
                             + (f"   (показаны первые {limit})" if len(found) > limit else ""))

    def _select_issue(self, iid):
        line, col, word = self._issue_pos[iid]
        a, b = f"{line}.{col}", f"{line}.{col + len(word)}"
        self.text.tag_remove("found", "1.0", "end")
        self.text.tag_add("found", a, b)
        self.text.see(a)
        self.text.mark_set("insert", a)
        return a, b, word

    def _goto_issue(self, event=None):
        sel = self.issues.selection()
        if sel and sel[0] in self._issue_pos:
            self._select_issue(sel[0])

    def _edit_issue(self, event=None):
        sel = self.issues.selection()
        if not sel or sel[0] not in self._issue_pos:
            return
        a, b, word = self._select_issue(sel[0])
        self._change_word(word, a, b, self.issues.item(sel[0], "values")[2])

    def _word_at(self, index):
        """Слово под курсором вместе с «+» (стандартный wordchars их не считает)."""
        line, col = map(int, self.text.index(index).split("."))
        s = self.text.get(f"{line}.0", f"{line}.end")
        for m in core.WORD_RE.finditer(s):
            if m.start() <= col <= m.end():
                return m.group(0), f"{line}.{m.start()}", f"{line}.{m.end()}"
        return None, None, None

    def _word_click(self, event):
        word, a, b = self._word_at(f"@{event.x},{event.y}")
        if not word or core.count_vowels(word) < 1:
            return
        line = a.split(".")[0]
        self._change_word(word, a, b, self.text.get(f"{line}.0", f"{line}.end")[:220])

    @staticmethod
    def _sub_same(m, plain, form):
        """Замена всех вхождений ТОГО ЖЕ написания. «все» -> «всё» не трогает
        уже стоящие «всё»: сравнение по букве, без нормализации ё."""
        w = m.group(0)
        flat = w.replace("+", "")
        if flat.lower() != plain.lower():
            return w
        out = form
        if flat[:1].isupper():
            out = ("+" + form[1].upper() + form[2:]) if form.startswith("+") \
                  else form[0].upper() + form[1:]
        return out

    def _change_word(self, word, a, b, ctx=""):
        dlg = StressDialog(self, word, ctx)
        self.wait_window(dlg)
        if not dlg.result:
            return
        form, scope = dlg.result
        plain = word.replace("+", "")
        if scope == "dict":
            val = ("+" + form[1].lower() + form[2:]) if form.startswith("+") \
                  else form[0].lower() + form[1:]
            self.dictionary[plain.lower()] = val
            core.save_dictionary(DICT_PATH, self.dictionary)
            self._refresh_dict()
            if self.acc_engine:
                self.acc_engine.set_dictionary(self.dictionary)
            self.log(f"В словарь: {plain.lower()} -> {val}")
        if scope in ("dict", "file"):
            data = self.text.get("1.0", "end-1c")
            new = core.WORD_RE.sub(lambda m: self._sub_same(m, plain, form), data)
            if new != data:
                pos = self.text.yview()[0]
                self.text.delete("1.0", "end")
                self.text.insert("1.0", new)
                self.text.yview_moveto(pos)
        else:
            self.text.delete(a, b)
            self.text.insert(a, form)
        self.text.edit_modified(True)
        # правку сразу закрепляем в файле с ударениями: «только здесь» тогда
        # попадёт в список правок и переживёт повторную расстановку
        if self.current_file and getattr(self, "_loaded_path", "") == self._acc_path(self.current_file):
            self.save_text()
        self.highlight()
        self.check_text()

    def set_font_size(self, size):
        self.font_size = max(7, min(28, int(size)))
        self.text.configure(font=("Segoe UI", self.font_size))
        self.settings["font_size"] = self.font_size

    def speak_text(self, text):
        """Прослушать одно слово или фразу (кнопка в диалоге ударения)."""
        text = (text or "").strip()
        if not text:
            return
        speaker, ssml, rate = self.voice_var.get(), self.ssml_var.get(), self._rate()
        model, threads = self.model_var.get(), self.threads_var.get()

        def job(ctx):
            synth = self._get_synth(ctx, model, threads)
            pcm = synth.say(text, speaker, "par", ssml, rate)
            core.write_wav(self._tmp_wav, pcm)
            return self._tmp_wav

        self.worker.start(job, on_done=self._play, name="проба")

    # -- словарь -------------------------------------------------------
    def _refresh_dict(self):
        self.dict_tree.delete(*self.dict_tree.get_children())
        for k in sorted(self.dictionary):
            self.dict_tree.insert("", "end", iid=k, values=(k, self.dictionary[k]))

    def dict_add(self):
        w, a = self.dw.get().strip(), self.da.get().strip()
        if not w:
            return
        if "+" not in a:
            messagebox.showwarning("Ударение", "В форме с ударением нужен «+» "
                                               "перед ударной гласной, например: пот+ом")
            return
        self.dictionary[w.lower()] = a
        core.save_dictionary(DICT_PATH, self.dictionary)
        self._refresh_dict()
        self.dw.set("")
        self.da.set("")
        if self.acc_engine:
            self.acc_engine.set_dictionary(self.dictionary)

    def dict_del(self):
        for k in self.dict_tree.selection():
            self.dictionary.pop(k, None)
        core.save_dictionary(DICT_PATH, self.dictionary)
        self._refresh_dict()
        if self.acc_engine:
            self.acc_engine.set_dictionary(self.dictionary)

    def dict_load(self):
        p = filedialog.askopenfilename(title="Словарь (.json)",
                                       filetypes=[("JSON", "*.json")], initialdir=APP_DIR)
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            messagebox.showerror("Словарь", str(e))
            return
        self.dictionary.update({k.lower(): v for k, v in d.items()})
        core.save_dictionary(DICT_PATH, self.dictionary)
        self._refresh_dict()
        if self.acc_engine:
            self.acc_engine.set_dictionary(self.dictionary)
        self.log(f"Словарь дополнен из {p}: всего слов {len(self.dictionary)}")

    def dict_export(self):
        p = filedialog.asksaveasfilename(defaultextension=".json",
                                         filetypes=[("JSON", "*.json")], initialdir=APP_DIR)
        if p:
            core.save_dictionary(p, self.dictionary)
            self.log(f"Словарь сохранён: {p}")

    # -- движки --------------------------------------------------------
    def _get_accentizer(self, ctx):
        # Словарь читаем с диска каждый раз: его правят и в другом окне, и
        # руками в файле. Иначе окно, открытое до правки, молча расставит
        # ударения по устаревшему словарю (так «С+омов» стал «Сом+овым»).
        on_disk = core.load_dictionary(DICT_PATH)
        if on_disk != self.dictionary:
            self.dictionary = on_disk
            ctx.log(f"Словарь перечитан: {len(on_disk)} слов")
            self.after(0, self._refresh_dict)
        if self.acc_engine is None:
            ctx.busy("Загружаю модели ударений (полгигабайта, это 10–20 секунд)…")
            ctx.log("Загружаю модели ударений: омографы, словарь, е/ё. "
                    "Первый раз это 10–20 секунд, дальше они уже в памяти.")
            cache = os.path.join(APP_DIR, "accents_cache.json")
            eng = core.Accentizer(self.dictionary, cache_path=cache, log=ctx.log)
            eng.load()
            self.acc_engine = eng
            # список спорных «е»/«ё» берём у самого RUAccent и сохраняем:
            # подсветка тогда работает и до загрузки моделей
            words = eng.yo_choice_words()
            if words:
                core.YO_AMBIGUOUS.update(words)
                core.save_yo_list(YO_LIST_PATH, words)
        else:
            self.acc_engine.set_dictionary(self.dictionary)
        return self.acc_engine

    def _get_synth(self, ctx, model_id, threads):
        """Параметры передаются явно: tkinter не потокобезопасен, читать
        переменные окна из фонового потока нельзя."""
        if self.synth is None or self.synth.model_id != model_id:
            ctx.busy(f"Загружаю голосовую модель {model_id} (пара секунд)…")
            self.synth = core.Synth(model_id, threads=threads, log=ctx.log)
        self.synth.threads = threads
        self.synth.load()
        return self.synth

    # -- операции ------------------------------------------------------
    def run_accent(self, all_files):
        files = self.selected_files(all_files)
        if not files:
            messagebox.showinfo("Нет файлов", "Сначала добавьте текстовые файлы.")
            return
        if self._dirty():
            self.save_text()

        workers = max(1, min(8, self.threads_var.get()))
        # Про загрузку моделей говорим сразу, до запуска работы: иначе первые
        # секунды окно молчит, и кажется, что нажатие не сработало.
        if self.acc_engine is None:
            self.set_indeterminate("Загружаю модели ударений…")
            self.show_overlay("Загружаю модели ударений\n\n"
                              "полгигабайта: омографы, словарь, е/ё\n"
                              "это 10–20 секунд и только при первом запуске\n\n"
                              "окно на это время перестанет отвечать — так и надо")
            self.log("Загружаю модели ударений — полгигабайта: омографы, "
                     "словарь, е/ё. Это только при первом запуске окна.")

        def job(ctx):
            acc = self._get_accentizer(ctx)
            t_all = time.time()
            for n, p in enumerate(files, 1):
                if ctx.cancelled:
                    break
                t_file = time.time()
                name = os.path.basename(p)
                with open(p, encoding="utf-8") as f:
                    src = f.read()
                dst = self._acc_path(p)
                edits = core.load_edits(dst)      # ручные правки главы имеют приоритет
                n_par = sum(1 for l in src.splitlines() if l.strip())
                ctx.log(f"{name}: строк {n_par}, потоков {workers}"
                        + (f", ручных правок {len(edits)}" if edits else "")
                        + (". Первый проход идёт минуты, дальше берётся из кэша"
                           if not acc.cache else ""))
                ctx.progress(0, max(1, n_par), name)
                out, kept = core.accent_file_text(
                    src, acc,
                    progress=lambda c, t, nm=name: ctx.progress(c, t, nm),
                    cancel=ctx.cancel, edits=edits, workers=workers)
                if ctx.cancelled:
                    break
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(out)
                acc.save_cache()
                ctx.log(f"Ударения расставлены: {os.path.basename(dst)} "
                        f"за {time.time() - t_file:.0f} с"
                        + (f"; ручных правок сохранено: {kept}" if kept else ""))
            if len(files) > 1:
                ctx.log(f"Всего {time.time() - t_all:.0f} с")
            return files

        self.worker.start(job, on_done=lambda r: (self._refresh_files(),
                                                  self._reload_view()),
                          name="расстановка ударений")

    def preview_selection(self):
        try:
            txt = self.text.get("sel.first", "sel.last").strip()
        except tk.TclError:
            line = self.text.index("insert").split(".")[0]
            txt = self.text.get(f"{line}.0", f"{line}.end").strip()
        if not txt:
            return
        if txt.startswith("#"):
            txt = txt.lstrip("#").strip()
        txt = txt[:900]
        speaker, ssml, rate = self.voice_var.get(), self.ssml_var.get(), self._rate()
        model, threads = self.model_var.get(), self.threads_var.get()

        def job(ctx):
            synth = self._get_synth(ctx, model, threads)
            body = txt if "+" in txt else self._get_accentizer(ctx).process(txt)
            ctx.log("· " + body[:160])
            pcm = synth.say(body, speaker, "par", ssml, rate)
            core.write_wav(self._tmp_wav, pcm)
            return self._tmp_wav

        self.worker.start(job, on_done=self._play, name="проба голоса")

    def preview_voice(self):
        speaker, ssml, rate = self.voice_var.get(), self.ssml_var.get(), self._rate()
        model, threads = self.model_var.get(), self.threads_var.get()

        def job(ctx):
            # фраза уже с ударениями: образец звучит одинаково независимо от
            # того, загружен ли RUAccent, и не ждёт его загрузки
            synth = self._get_synth(ctx, model, threads)
            pcm = synth.say(core.SAMPLE_PHRASE, speaker, "par", ssml, rate)
            core.write_wav(self._tmp_wav, pcm)
            return self._tmp_wav

        self.worker.start(job, on_done=self._play, name="образец голоса")

    def _play(self, path):
        if not path or not os.path.exists(path):
            return
        try:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            os.startfile(path)

    def _rate(self):
        return {"обычный": None, "медленно": "slow", "быстро": "fast"}[self.rate_var.get()]

    def ensure_ffmpeg(self):
        """Если ffmpeg не найден, предлагаем скачать официальную LGPL-сборку.

        Внутрь приложения его не кладём: распространяемые готовые сборки
        собраны под GPL. Без ffmpeg тоже можно работать -- главы просто
        останутся в WAV, без сжатия и выравнивания громкости."""
        if core.ffmpeg_exe():
            return True
        ans = messagebox.askyesnocancel(
            "Нужен ffmpeg",
            f"Для mp3 и выравнивания громкости нужен ffmpeg "
            f"({core.FFMPEG_SIZE_MB} МБ).\n\n"
            "Скачать его сейчас? Он ляжет в папку рядом с приложением "
            "и больше не понадобится.\n\n"
            "«Нет» — озвучить в WAV без сжатия.")
        if ans is None:
            return False
        if not ans:
            return True

        def job(ctx):
            return core.download_ffmpeg(
                progress=lambda c, t, _n: ctx.progress(c, t, "ffmpeg"),
                log=ctx.log, cancel=ctx.cancel)

        self.set_indeterminate("Скачиваю ffmpeg…")
        self.worker.start(job, name="загрузка ffmpeg",
                          on_done=lambda p: self.log(
                              "ffmpeg готов, можно озвучивать." if p
                              else "ffmpeg не установлен — озвучка будет в WAV."))
        return False        # пусть скачается, озвучку запустит следующим нажатием

    def run_render(self, all_files):
        files = self.selected_files(all_files)
        if not files:
            messagebox.showinfo("Нет файлов", "Сначала добавьте текстовые файлы.")
            return
        if not self.ensure_ffmpeg():
            return
        if self._dirty():
            self.save_text()
        out_dir = self.out_var.get()
        os.makedirs(out_dir, exist_ok=True)
        opts = dict(pause_par=self.p_par.get(), pause_title=self.p_title.get(),
                    pause_break=self.p_brk.get(), ssml=self.ssml_var.get(),
                    rate=self._rate())
        speaker = self.voice_var.get()
        album = self.album_var.get()
        artist = self.artist_var.get()
        loud = self.loud_var.get()
        merge = self.merge_var.get()
        use_acc = self.useacc_var.get()
        split = self.split_var.get()
        model, threads = self.model_var.get(), self.threads_var.get()
        if self.synth is None:
            self.set_indeterminate("Загружаю голосовую модель…")
            self.show_overlay("Загружаю голосовую модель\n\n"
                              "несколько секунд, только при первом запуске")

        def job(ctx):
            import time
            ctx.log(f"Озвучка: файлов {len(files)}, голос {core.VOICES.get(speaker, speaker)},"
                    f" модель {model}, потоков {threads}"
                    + (", делим по главам" if split else ""))
            synth = self._get_synth(ctx, model, threads)
            acc = None
            made = []
            track = 0
            t0 = time.time()
            for n, p in enumerate(files, 1):
                if ctx.cancelled:
                    break
                src_path = self._acc_path(p) if (use_acc and os.path.exists(self._acc_path(p))) else p
                with open(src_path, encoding="utf-8") as f:
                    data = f.read()
                marked = "+" in data
                if not marked:
                    acc = acc or self._get_accentizer(ctx)
                    ctx.log(f"{os.path.basename(p)}: ударения на лету")
                sections = core.parse_text(data, split_chapters=split)
                stem = os.path.splitext(os.path.basename(p))[0]
                n_units = sum(len(core.split_long(t)) for _, items in sections
                              for k, t in items if k != "brk")
                ctx.log(f"{os.path.basename(p)}: частей {len(sections)}, "
                        f"фрагментов {n_units}")
                for title, items in sections:
                    if ctx.cancelled:
                        break
                    if not marked and acc is not None:
                        items = [(k, (acc.process(t) if t else t)) for k, t in items]
                    track += 1
                    title = title.replace("+", "")     # в тегах и именах файлов
                    ctx.status(f"озвучка: {title} ({n}/{len(files)})")
                    if len(sections) > 1:
                        ctx.log(f"  {track:02d} читаю: {title}")
                    pcm = core.render_section(
                        synth, items, speaker, opts,
                        progress=lambda c, t, lbl: ctx.progress(c, t, lbl),
                        cancel=ctx.cancel)
                    if ctx.cancelled:
                        break
                    name = safe_name(title) if len(sections) > 1 else stem
                    base = os.path.join(out_dir, f"{track:02d}_{name}")
                    core.write_wav(base + ".wav", pcm)
                    ok = core.to_mp3(base + ".wav", base + ".mp3", title, track,
                                     album=album, artist=artist, loudnorm=loud,
                                     log=ctx.log)
                    if ok:
                        os.remove(base + ".wav")
                    made.append(base + (".mp3" if ok else ".wav"))
                    mins = len(pcm) / core.SAMPLE_RATE / 60
                    ctx.log(f"{track:02d} {title}: {mins:.1f} мин -> {os.path.basename(made[-1])}")
                if acc is not None:
                    acc.save_cache()
            if merge and len(made) > 1 and not ctx.cancelled:
                dst = os.path.join(out_dir, safe_name(album or "audiobook") + "_all.mp3")
                if core.merge_mp3(made, dst, log=ctx.log):
                    ctx.log(f"Общий файл: {dst}")
            ctx.log(f"Всего {time.time() - t0:.0f} с")
            return out_dir

        self.worker.start(job, on_done=lambda r: self.log("Готово."), name="озвучка")

    def pick_out(self):
        d = filedialog.askdirectory(title="Папка для аудио",
                                    initialdir=self.out_var.get() or APP_DIR)
        if d:
            self.out_var.set(d)

    def open_out(self):
        d = self.out_var.get()
        os.makedirs(d, exist_ok=True)
        os.startfile(d)

    # -- настройки -----------------------------------------------------
    def _load_settings(self):
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings(self):
        s = self.settings
        s.update(files=self.files, voice=self.voice_var.get(), model=self.model_var.get(),
                 rate=self.rate_var.get(), threads=self.threads_var.get(),
                 pause_par=self.p_par.get(), pause_title=self.p_title.get(),
                 pause_break=self.p_brk.get(), ssml=self.ssml_var.get(),
                 loudnorm=self.loud_var.get(), merge=self.merge_var.get(),
                 use_acc=self.useacc_var.get(), split_chapters=self.split_var.get(),
                 album=self.album_var.get(), artist=self.artist_var.get(),
                 out_dir=self.out_var.get(), font_size=self.font_size)
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _quit(self):
        if self.worker.busy:
            if not messagebox.askyesno("Выход", "Идёт работа. Прервать и выйти?"):
                return
            self.worker.stop()
        if self._dirty():
            if messagebox.askyesno("Сохранить?", "Текст изменён. Сохранить перед выходом?"):
                self.save_text()
        self._save_settings()
        if self.acc_engine:
            self.acc_engine.save_cache()
        self.destroy()


def run_selftest():
    """Проверка сборки без окна: `Аудиокнига.exe --selftest`.

    Собранное приложение запускается без консоли, поэтому отчёт пишется в
    файл `selftest.log` рядом с exe. Проверяются ровно те три вещи, которые
    ломаются при сборке: модели ударений, голосовая модель и ffmpeg."""
    import tempfile
    import traceback
    lines, ok = [], True
    root = core.app_root()

    def log(m):
        lines.append(str(m))

    try:
        log(f"папка приложения: {root}")
        log(f"настройки и аудио: {APP_DIR}")
        log(f"модель Silero:    {core.find_model_file(core.DEFAULT_MODEL)}")
        log(f"модели RUAccent:  {core.ruaccent_workdir()}")
        log(f"ffmpeg:           {core.FFMPEG}")

        eng = core.Accentizer(core.load_dictionary(DICT_PATH), log=log)
        if not eng.load():
            ok = False
        # Фраза-проба заодно проверяет правила, которые легко сломать
        # незаметно: «что» после двоеточия без знака, «то» со знаком,
        # постфикс «-то» без знака, число словами, «ё» со знаком.
        probe = ("Он пошёл в мастерскую: что там, 15 лет никто не был? "
                 "И то, что он увидел, было что-то новое.")
        marked = eng.process(probe)
        log(f"ударения: {marked}")
        for must in ("пятн+адцать", "пош+ёл", "и т+о,", "чт+о-то"):
            if must not in marked.lower():
                log(f"ОШИБКА: в разметке нет «{must}»")
                ok = False

        # Омограф в начале предложения: заглавная буква отключает его разбор
        # у RUAccent, и «Потом» читается «по́том», через пот. Ловится такое
        # только на слух: знак стоит по всем правилам, просто не на том
        # слоге, и при чтении файла глазами за него не цепляешься.
        head = eng.process("Потом были худые годы.")
        if not head.startswith("Пот+ом"):
            log(f"ОШИБКА: омограф в начале предложения -- {head}")
            ok = False

        # Ручная правка не должна замораживать строку: поздние правила
        # обязаны доходить до неё. Слово человека при этом неприкосновенно.
        merged = core.merge_edit("Пот+ом он вз+ял козл+ы",      # свежая
                                 "П+отом он вз+ял к+озлы",      # правка
                                 "П+отом он вз+ял козл+ы")      # база
        if merged != "Пот+ом он вз+ял к+озлы":
            log(f"ОШИБКА: ручная правка замораживает строку -- {merged}")
            ok = False

        # Перенос ударения на проклитику -- вместе с исключением: женский
        # род ударение сохраняет, и именно эта половина правила ломается
        # незаметно (граница слова \b считает границей знак «+»).
        neg = eng.process("Рации не было, и связи не была.")
        if "н+е было" not in neg or "не был+а" not in neg:
            log(f"ОШИБКА: перенос ударения на «не» -- {neg}")
            ok = False

        # Интонация живёт в SSML, а не в разметке ударений, и сломать её
        # можно так, что ни одна буква в .acc.txt не изменится.
        rise = core.build_ssml("— Ты пойд+ёшь?", "par")
        flat = core.build_ssml("— Почем+у ты молч+ишь?", "par")
        if 'pitch="x-high"' not in rise or 'pitch="x-high"' in flat:
            log("ОШИБКА: вопросительная интонация не та")
            ok = False

        # Модель берём ту же, что и программа: раньше здесь была зашита
        # v5_5_ru, которой в свободной сборке нет -- самопроверка на чужой
        # машине полезла бы за ней в сеть.
        model = core.DEFAULT_MODEL
        synth = core.Synth(model, threads=2, log=log)
        pcm = synth.say(marked, next(iter(core.voices_for(model))))
        log(f"синтез: {len(pcm)/core.SAMPLE_RATE:.2f} c звука")
        ok = ok and len(pcm) > core.SAMPLE_RATE

        wav = os.path.join(tempfile.gettempdir(), "audiobook_selftest.wav")
        mp3 = wav[:-4] + ".mp3"
        core.write_wav(wav, pcm)
        if core.ffmpeg_exe():
            made = core.to_mp3(wav, mp3, "Проверка", 1, log=log)
            log(f"mp3: {'собран' if made else 'НЕ СОБРАН'}")
            ok = ok and made
        else:
            # в свежей сборке ffmpeg ещё не скачан -- это не поломка
            log("mp3: не проверен, ffmpeg пока не установлен "
                "(программа предложит скачать его при первой озвучке)")
    except Exception:
        ok = False
        lines.append(traceback.format_exc())

    lines.append("ИТОГ: " + ("всё работает" if ok else "ЕСТЬ ОШИБКИ"))
    try:
        with open(os.path.join(root, "selftest.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(run_selftest())
    App().mainloop()
