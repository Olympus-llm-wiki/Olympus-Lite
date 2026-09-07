# Olympus Lite

**Стартер файловой вики без Hindsight, Docker и отдельного сервера.**
**Источники, связанные знания и история решений остаются обычными файлами.**
**В комплекте только программа и синтетические примеры; личную вики создаёт пользователь.**

Для варианта с Hindsight используйте [Olympus Max](https://github.com/Olympus-llm-wiki/Olympus-Max).

## Обновление 0.2.0

Добавлены [25 навыков и медиа-навык](docs/skills.md), переносимые инструкции Codex/Claude и проверка состава поставки. Файловый CLI сохраняет работу без сети и модельных зависимостей.

## Начать

Нужен Python 3.11+. Скачайте репозиторий в отдельную папку:

```bash
python3 olympus.py init ~/Documents/My-Olympus --title "Моя библиотека"
python3 olympus.py --vault ~/Documents/My-Olympus status
```

Откройте созданную папку в Obsidian или обычном редакторе и начните с Home.md. Агент с доступом к файлам помогает читать, связывать и обновлять материалы; модель выбирается пользователем. Сам Lite не вызывает модели и не требует API key.

Для Windows используйте установленный Python, например `py -3 olympus.py`; нативная Windows пока не испытана.

| Раздел | Содержимое |
|---|---|
| Home | Вход в библиотеку |
| Knowledge | Понятия, темы, проекты и инструкции |
| Sources | Полные оригиналы и версии |
| Drafts | Предложения страниц до принятия |
| Journal | История применённых изменений |

## Перенести старую вики

```bash
python3 olympus.py inventory /path/to/old-wiki --output /path/to/inventory.json
python3 olympus.py plan /path/to/old-wiki --namespace my-old-wiki --select 'Chosen folder' --output /path/to/plan.json
python3 olympus.py --vault /path/to/my-wiki import /path/to/plan.json
```

Переносятся только выбранные Markdown/TXT. Исходная папка не изменяется, повтор не создаёт дубликатов. Импорт не принимает прежние инструкции или статусы verified/canonical как действующее подтверждение. [Подробности импорта](docs/import.md).

## Посмотреть готовый пример

```bash
python3 scripts/demo.py --output /path/to/new-demo-directory
```

Откройте Library/Home.md внутри примера. Это полностью синтетические источники и решения, созданные для демонстрации.

[Рабочий цикл с агентом](docs/quickstart.md) · [Архитектура и ограничения](docs/architecture.md) · [Происхождение исходников](TEMPLATE.json).

Поиск работает по словам, без векторов и Hindsight Reflect. Совпадение цитаты с оригиналом не доказывает истинность вывода. Храните личную библиотеку отдельно от репозитория продукта.

## Проверка

```bash
python3 -m unittest discover -s tests -q
```

Runtime использует только стандартную библиотеку Python. Для запуска из checkout установка дополнительных пакетов не нужна.

Проверка состава: `python3 scripts/verify-distribution.py`. [Как обновлять поставку](docs/updating.md).
