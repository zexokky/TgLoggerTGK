#!/usr/bin/env python3
"""tglog: пишет пост-ченджлог для Telegram-канала в твоём стиле.

  python tglog.py learn @канал           изучить стиль канала (один раз)
  python tglog.py post /путь/к/проекту   пост по коммитам с последнего тега
  python tglog.py new /путь/к/проекту    анонс нового проекта (README + список файлов)
"""
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import anthropic
    from telethon import TelegramClient, errors
except ImportError:
    sys.exit("❌ Не хватает пакетов. Выполни: source venv/bin/activate && pip install telethon anthropic")

BASE = Path(__file__).resolve().parent
POSTS, STYLE, EXAMPLES, CHANNEL = (BASE / n for n in ("posts.json", "style.md", "examples.json", "channel.txt"))
SONNET = "claude-sonnet-4-6"  # один раз изучает стиль
HAIKU = "claude-haiku-4-5"  # пишет посты
JUNK = re.compile(
    r"(^|/)(\.git|\.idea|\.vscode|node_modules|venv|\.venv|__pycache__|dist|build)(/|$)"
    r"|\.lock$|package-lock\.json$|pnpm-lock\.yaml$|go\.sum$|\.min\.(js|css)$"
)
LEARN_PROMPT = (
    "Ниже пронумерованные посты Telegram-канала одного автора. Сделай два дела.\n"
    "1) ПЕРВОЙ строкой ответа напиши «ПРИМЕРЫ:» и через запятую номера 8 самых типичных постов, "
    "самый типичный первым. Если среди постов есть сообщения об обновлениях проектов, "
    "выбирай примеры в первую очередь из них.\n"
    "2) Дальше опиши стиль автора: тон, длина постов, эмодзи, структура, как оформляет списки, "
    "как начинает и как заканчивает посты, какое использует форматирование. "
    "Пиши как инструкцию для другого автора, который должен писать так же. "
    "Максимум 300 слов, без вступлений.\n\n"
)


def die(msg):
    sys.exit("❌ " + msg)


def load_env():
    env = BASE / ".env"
    if not env.exists():
        die("Нет файла .env. Выполни: cp .env.example .env — и впиши туда ключи (nano .env).")
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip("\"'")
    for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "ANTHROPIC_API_KEY"):
        if not os.environ.get(k):
            die(f"В файле .env не заполнено поле {k}.")


def tg():
    try:
        return TelegramClient(str(BASE / "tglog"), int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])
    except ValueError:
        die("TELEGRAM_API_ID в .env должен быть числом.")


def ask(model, user, system=None, max_tokens=500):
    kw = {}
    if system:  # system кэшируется (если он достаточно длинный, иначе API кэш просто пропускает)
        kw["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    try:
        r = anthropic.Anthropic().messages.create(
            model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": user}], **kw
        )
    except anthropic.AuthenticationError:
        die("Неверный ANTHROPIC_API_KEY — проверь файл .env.")
    except anthropic.APIConnectionError:
        die("Нет связи с Anthropic API — проверь интернет.")
    except anthropic.APIStatusError as e:
        die(f"Ошибка Anthropic API ({e.status_code}): {e.message}")
    u = r.usage
    print(f"   [токены: вход {u.input_tokens}, из кэша {u.cache_read_input_tokens or 0}, "
          f"записано в кэш {u.cache_creation_input_tokens or 0}, выход {u.output_tokens}]")
    if r.stop_reason == "max_tokens":
        print("⚠️  Ответ упёрся в лимит токенов и мог обрезаться.")
    return r.content[0].text.strip()


async def fetch_posts(channel):
    posts = []
    async with tg() as client:  # при первом запуске спросит номер телефона и код из Telegram
        async for m in client.iter_messages(channel, limit=300):
            if m.text:
                text = re.sub(r"\[([^\]]*)\]\(emoji/\d+\)", r"\1", m.text)  # кастомные эмодзи -> обычные
                posts.append({"id": m.id, "date": m.date.isoformat(), "text": text})
                if len(posts) == 100:
                    break
    return posts


async def send_post(channel, text):
    async with tg() as client:
        await client.send_message(channel, text)


def run_tg(coro):
    try:
        return asyncio.run(coro)
    except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError, ValueError):
        die("Канал не найден — проверь имя (пример: @mychannel).")


def git(path, *args):
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        die("git не установлен. Поставь: sudo apt install git")
    return r.returncode, (r.stdout.strip() or r.stderr.strip())


def system_text():
    if not (STYLE.exists() and EXAMPLES.exists()):
        die("Сначала нужно изучить канал: python tglog.py learn @канал")
    ex = json.loads(EXAMPLES.read_text(encoding="utf-8"))[:3]
    return (
        "Ты пишешь посты для Telegram-канала автора от его имени, строго в его стиле. "
        "Выводи только текст поста, без пояснений. Разметка как в примерах (**жирный**, __курсив__, `код`). "
        "Опирайся только на данные из запроса, ничего не выдумывай.\n\n"
        "СТИЛЬ АВТОРА:\n" + STYLE.read_text(encoding="utf-8").strip()
        + "\n\nПРИМЕРЫ ПОСТОВ АВТОРА:\n\n" + "\n\n---\n\n".join(t[:1500] for t in ex)
    )


def review(draft):
    while True:
        print("\n" + "=" * 40 + "\n" + draft + "\n" + "=" * 40)
        choice = input("1 — опубликовать, 2 — переделать, 3 — отмена: ").strip()
        if choice == "1":
            return draft
        if choice == "3":
            print("Отменено, ничего не опубликовано.")
            sys.exit(0)
        if choice == "2":
            note = input("Что поправить: ").strip()
            if note:  # уходит только черновик и комментарий
                draft = ask(
                    HAIKU,
                    f"Черновик поста:\n\n{draft}\n\nПравка: {note}\n\nПерепиши пост с учётом правки. Только текст поста.",
                    "Ты редактор постов для Telegram. Сохраняй стиль автора и разметку (**жирный**, __курсив__, `код`).",
                )


def publish(text):
    if not CHANNEL.exists():
        die("Не знаю, в какой канал писать. Сначала выполни: python tglog.py learn @канал")
    channel = CHANNEL.read_text(encoding="utf-8").strip()
    run_tg(send_post(channel, text))
    print(f"✅ Опубликовано в {channel}")


def learn(channel):
    print(f"Скачиваю посты из {channel}...")
    posts = run_tg(fetch_posts(channel))
    if not posts:
        die("В канале не нашлось текстовых постов.")
    POSTS.write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Скачано постов: {len(posts)}. Анализирую стиль (один запрос к Claude)...")
    numbered = "\n\n".join(f"#{i}\n{p['text'][:1000]}" for i, p in enumerate(posts, 1))
    # 300 слов по-русски не влезают в 500 токенов, поэтому здесь лимит больше (запрос делается один раз)
    out = ask(SONNET, LEARN_PROMPT + numbered, max_tokens=1200)
    m = re.search(r"ПРИМЕРЫ[:*\s]*[\d,#№ ]+\**", out)
    nums = [int(n) for n in re.findall(r"\d+", m.group())] if m else []
    style = (out[:m.start()] + out[m.end():]).strip() if m else out
    good = []
    for n in nums:
        if 1 <= n <= len(posts) and n not in good:
            good.append(n)
    rest = [n for n in range(1, len(posts) + 1) if n not in good]  # добиваем до 8, если Claude дал меньше
    examples = [posts[n - 1]["text"] for n in (good + rest)[:8]]
    STYLE.write_text(style + "\n", encoding="utf-8")
    EXAMPLES.write_text(json.dumps(examples, ensure_ascii=False, indent=1), encoding="utf-8")
    CHANNEL.write_text(channel + "\n", encoding="utf-8")
    print(f"✅ Готово: style.md ({len(style.split())} слов), examples.json ({len(examples)} постов). "
          f"Канал запомнен: {channel}")


def post(path):
    system = system_text()
    if not os.path.isdir(path):
        die(f"Папка не найдена: {path}")
    name = Path(path).resolve().name
    tcode, tag = git(path, "describe", "--tags", "--abbrev=0")
    rng = [f"{tag}..HEAD", "-n", "50"] if tcode == 0 else ["-n", "30"]
    code, out = git(path, "log", *rng, "--no-merges", "--name-only", "--pretty=format:@@%s")
    if code:
        die(f"git не смог прочитать историю: {out}\nЕсли это новый проект без коммитов, используй команду new.")
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = (line[2:].strip()[:150], [])
            commits.append(cur)
        elif line.strip() and cur:
            cur[1].append(line.strip())
    # список файлов нужен только чтобы отсеять мусорные коммиты (lock-файлы и т.п.), в Claude он не уходит
    lines = [s for s, files in commits if s and not (files and all(JUNK.search(f) for f in files))]
    since = f"с тега {tag}" if tcode == 0 else "последние 30 коммитов"
    if not lines:
        die(f"Нет новых коммитов ({since}) — писать не о чем.")
    user = f"Проект: {name}\nКоммиты ({since}):\n" + "\n".join("- " + s for s in lines) + "\n\nНапиши пост-ченджлог."
    print(f"Коммитов в запросе: {len(lines)} ({since}). Пишу черновик...")
    publish(review(ask(HAIKU, user, system)))


def new(path):
    system = system_text()
    if not os.path.isdir(path):
        die(f"Папка не найдена: {path}")
    root = Path(path).resolve()
    readme = next((p for p in sorted(root.iterdir()) if p.is_file() and p.name.lower().startswith("readme")), None)
    text = readme.read_text(encoding="utf-8", errors="ignore")[:3000] if readme else "(README не найден)"
    files = []
    for d, dirs, names in os.walk(root):
        rel = Path(d).relative_to(root)
        dirs[:] = [] if len(rel.parts) >= 3 else sorted(x for x in dirs if not x.startswith(".") and not JUNK.search(x))
        files += [str(rel / n) for n in sorted(names) if not n.startswith(".") and not JUNK.search(n)]
    user = (f"Это новый проект «{root.name}», коммитов ещё нет. Напиши пост-анонс.\n\n"
            f"README:\n{text}\n\nФайлы проекта:\n" + "\n".join(files[:80]))
    print("Пишу анонс...")
    publish(review(ask(HAIKU, user, system)))


def main():
    cmds = {"learn": learn, "post": post, "new": new}
    args = sys.argv[1:]
    if len(args) != 2 or args[0] not in cmds:
        sys.exit("Как пользоваться:\n  python tglog.py learn @канал\n"
                 "  python tglog.py post /путь/к/проекту\n  python tglog.py new /путь/к/проекту")
    load_env()
    try:
        cmds[args[0]](args[1])
    except KeyboardInterrupt:
        sys.exit("\nПрервано.")
    except errors.ApiIdInvalidError:
        die("Неверные TELEGRAM_API_ID / TELEGRAM_API_HASH — проверь .env (ключи берутся на my.telegram.org).")
    except errors.FloodWaitError as e:
        die(f"Telegram просит подождать {e.seconds} сек. и попробовать снова.")
    except (errors.ChatAdminRequiredError, errors.ChatWriteForbiddenError):
        die("В этот канал нельзя писать: аккаунт должен быть админом канала с правом публикации.")
    except errors.RPCError as e:
        die(f"Ошибка Telegram: {e}")
    except ConnectionError as e:
        die(f"Нет связи с Telegram — проверь интернет. ({e})")
    except Exception as e:
        die(f"Неожиданная ошибка: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
