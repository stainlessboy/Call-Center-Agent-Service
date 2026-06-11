# Подключение Confluence к Claude Code через MCP

Эта инструкция настраивает интеграцию **Claude Code** с корпоративным Confluence (`wiki.asakabank.uz`). После настройки Claude умеет читать, искать, создавать, обновлять, удалять страницы и комментарии прямо из чата.

Инструкция написана под **Windows**. На Mac/Linux логика та же, отличаются только пути и синтаксис shell (везде, где видите PowerShell, используйте bash/zsh).

---

## Что вы получаете

- ~25 MCP-инструментов для работы с Confluence: `search`, `get_page`, `create_page`, `update_page`, `delete_page`, работа с комментариями, метками, вложениями, иерархией страниц и пр.
- Настройка **глобальная** (user-scope) — работает во **всех** ваших проектах Claude Code автоматически, не нужно править конфиг в каждом репо.
- Токен живёт в Windows User ENV, не в коде/репо/`.env`-файлах.

---

## Что понадобится

1. **Python 3.10+**, доступный в PATH (`python --version`)
2. **Claude Code** (CLI или VSCode-расширение)
3. **Учётка в `wiki.asakabank.uz`** с правом создавать Personal Access Tokens (обычно есть у всех)
4. Доступ к **PowerShell** на своей машине

---

## 🚀 Быстрый путь — через Claude Code (рекомендуется)

Если у вас уже установлен Claude Code, **бо́льшую часть настройки сделает он сам**. От вас нужно:

1. **Установить MCP-сервер** (один раз):
   ```powershell
   pip install --user mcp-atlassian
   ```

2. **Создать Personal Access Token** в Confluence (детально см. «Шаг 2» в ручной установке ниже). Токен показывается **один раз** — скопируйте сразу.

3. **Создать `.env`** в корне любого проекта, открываемого в Claude Code. Заготовка — в `.env.example` рядом с этим файлом. Скопируйте её и впишите свой токен:
   ```env
   CONFLUENCE_URL=https://wiki.asakabank.uz
   CONFLUENCE_PERSONAL_TOKEN=<сюда_токен>
   ```

4. **Откройте этот проект в Claude Code и попросите:**
   > Прочитай CONFLUENCE_MCP_SETUP.md и настрой Confluence MCP по моему .env

5. **Перезапустите Claude Code** — он попросит это сделать на финальном шаге.

После рестарта попросите в чате «найди в Confluence любую страницу» — увидите реальные страницы, значит всё работает.

> `.env` здесь — **временный мост** для передачи токена в Claude Code. После того как Claude перенесёт значения в Windows User ENV, файл удаляется. Подробнее — секция [«А что насчёт `.env`-файлов?»](#а-что-насчёт-env-файлов) ниже.

### Что Claude Code сделает по этой инструкции

Этот блок — действия **для Claude Code** при выполнении просьбы из шага 4. Если вы человек — читайте как описание происходящего; если Claude — выполняйте по порядку, останавливаясь и сообщая пользователю при первой ошибке.

1. **Прочитать `.env`** в корне проекта, извлечь `CONFLUENCE_URL` и `CONFLUENCE_PERSONAL_TOKEN`. Если файла нет или ключи пусты — остановиться и попросить пользователя создать `.env` из `.env.example`.

2. **Проверить токен** через `GET $CONFLUENCE_URL/rest/api/user/current` с заголовком `Authorization: Bearer $token`. Ожидать в ответе `"type":"known"` и непустой `"username"`. Если `"type":"anonymous"` или 401/403 — остановиться и сказать пользователю: «токен невалиден или неполный (должен быть ~44 символа); пересоздай PAT в Confluence и обнови `.env`».

3. **Записать значения в Windows User ENV** (на macOS/Linux — в `~/.zshrc` или `~/.bashrc` соответственно):
   ```powershell
   [Environment]::SetEnvironmentVariable('CONFLUENCE_URL', $url, 'User')
   [Environment]::SetEnvironmentVariable('CONFLUENCE_PERSONAL_TOKEN', $tok, 'User')
   ```

4. **Найти путь к `mcp-atlassian.exe`**:
   ```powershell
   $scripts = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
   $exe = Join-Path $scripts 'mcp-atlassian.exe'
   Test-Path $exe   # должно вернуть True
   ```
   Если бинарь не найден — попросить пользователя запустить `pip install --user mcp-atlassian` и повторить.

5. **Зарегистрировать MCP-сервер на user-scope** (одинарные кавычки в `-e` обязательны — чтобы shell не раскрыл `${...}`):
   ```bash
   claude mcp add atlassian --scope user \
     -e 'CONFLUENCE_URL=${CONFLUENCE_URL}' \
     -e 'CONFLUENCE_PERSONAL_TOKEN=${CONFLUENCE_PERSONAL_TOKEN}' \
     -- "<путь_к_mcp-atlassian.exe>"
   ```
   Если такой сервер уже зарегистрирован под этим именем — сначала `claude mcp remove atlassian -s user`, потом `add`.

6. **Подтвердить регистрацию**: `claude mcp list` должен показать `atlassian: ... - ✓ Connected`.

7. **Удалить `.env`** — токен теперь в Windows User ENV, файл больше не нужен и только повышает риск утечки. Перед удалением спросить подтверждение у пользователя.

8. **Сообщить пользователю**: «Готово. Закрой Claude Code полностью (через File → Exit, не крестик) и открой заново. После рестарта попроси меня найти любую страницу в Confluence — это финальная проверка.»

Если что-то пошло не так на любом шаге — переходите к **ручной установке** ниже.

---

## Ручная установка

Используйте этот раздел, если быстрый путь не сработал или вы хотите видеть все команды.

### 1. Установить MCP-сервер `mcp-atlassian`

В PowerShell:
```powershell
pip install --user mcp-atlassian
```

Узнайте, куда установился исполняемый файл:
```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```
Получите путь вида `C:\Users\<вы>\AppData\Roaming\Python\Python3XX\Scripts`. **Запомните его** — он понадобится на шаге 4. Проверьте, что бинарь на месте:
```powershell
Test-Path "C:\Users\<вы>\AppData\Roaming\Python\Python3XX\Scripts\mcp-atlassian.exe"
```
Должно вернуть `True`.

### 2. Создать Personal Access Token (PAT) в Confluence

1. Откройте `https://wiki.asakabank.uz` и авторизуйтесь.
2. Клик по аватарке (правый верхний угол) → **Profile** → **Personal Access Tokens** (или **Личные маркеры доступа**).
3. **Create token** / **Создать маркер**:
   - **Имя**: `claude-code-mcp`
   - **Срок действия**: 90 дней (или больше, по политике безопасности компании)
4. **Скопируйте значение СРАЗУ** — Confluence показывает токен только один раз, после закрытия диалога он становится недоступен.
5. Сохраните токен в менеджере паролей (Bitwarden, 1Password, KeePassXC) — это ваш единственный backup.

> **Sanity-check:** валидный PAT длиной **~44 символа** (base64-like). Если значение короче — скорее всего вы скопировали не до конца. Создайте заново.

### 3. Записать токен и URL в Windows User ENV

```powershell
[Environment]::SetEnvironmentVariable('CONFLUENCE_URL','https://wiki.asakabank.uz','User')
[Environment]::SetEnvironmentVariable('CONFLUENCE_PERSONAL_TOKEN','<вставьте_токен_сюда>','User')
```

Проверьте, что записалось корректно:
```powershell
[Environment]::GetEnvironmentVariable('CONFLUENCE_URL','User')
([Environment]::GetEnvironmentVariable('CONFLUENCE_PERSONAL_TOKEN','User')).Length
```
Ожидается URL и длина `44`.

### 4. Подключить MCP-сервер к Claude Code на user-scope

Подставьте свой путь из шага 1:
```powershell
claude mcp add atlassian --scope user `
  -e 'CONFLUENCE_URL=${CONFLUENCE_URL}' `
  -e 'CONFLUENCE_PERSONAL_TOKEN=${CONFLUENCE_PERSONAL_TOKEN}' `
  -- "C:\Users\<вы>\AppData\Roaming\Python\Python3XX\Scripts\mcp-atlassian.exe"
```

> **Важно:** значения в `-e` берите в **одинарные** кавычки — это говорит PowerShell не раскрывать `${...}` сразу, а передать в Claude Code как placeholder для подстановки во время запуска MCP-сервера.

### 5. Перезапустить Claude Code

Полностью **закройте** VSCode или CLI-окно Claude Code и откройте заново. MCP-процесс читает env только при старте.

---

## Проверка

### Способ A — статус MCP-серверов

В терминале:
```powershell
claude mcp list
```
Среди списка должна появиться строка:
```
atlassian: C:\Users\<вы>\...\mcp-atlassian.exe - ✓ Connected
```

### Способ B — реальный запрос

В чате Claude Code напишите:
> Найди в Confluence любую страницу

Должны вернуться реальные страницы с заголовками и ссылками.

### Способ C — прямая проверка токена через REST (без Claude Code)

```powershell
$tok = [Environment]::GetEnvironmentVariable('CONFLUENCE_PERSONAL_TOKEN','User')
(Invoke-WebRequest -Uri 'https://wiki.asakabank.uz/rest/api/user/current' `
  -Headers @{Authorization="Bearer $tok"} -UseBasicParsing).Content
```
Ожидается JSON с `"type":"known"` и вашим `"username"`. Если видите `"type":"anonymous"` — токен не работает (см. troubleshooting).

---

## Использование

После настройки можно просить Claude:
- «Найди в Confluence страницы про X»
- «Прочитай страницу с id 12345»
- «Покажи дерево страниц в space RTRD»
- «Создай страницу в space `~<мой_username>` с заголовком Y и таким-то содержимым»
- «Прокомментируй страницу X»
- «Скачай вложение N со страницы Y»

Claude сам выберет нужный MCP-инструмент.

---

## Ротация токена

PAT истекает раз в N дней. За 1–2 недели до истечения:
1. Создайте новый токен в Confluence (повторите шаг 2 этой инструкции)
2. Обновите Windows User ENV:
   ```powershell
   [Environment]::SetEnvironmentVariable('CONFLUENCE_PERSONAL_TOKEN','<новый_токен>','User')
   ```
3. Отзовите старый токен в Confluence
4. Перезапустите Claude Code

---

## Подключение Jira (опционально)

Jira (`jira.asakabank.uz`) — отдельный продукт Atlassian, ему нужен **отдельный** PAT. Тот же MCP-сервер `mcp-atlassian` умеет работать с Jira и Confluence одновременно.

1. Создайте PAT в Jira (Profile → Personal Access Tokens на `https://jira.asakabank.uz`)
2. Добавьте две переменные:
   ```powershell
   [Environment]::SetEnvironmentVariable('JIRA_URL','https://jira.asakabank.uz','User')
   [Environment]::SetEnvironmentVariable('JIRA_PERSONAL_TOKEN','<jira_токен>','User')
   ```
3. Пересоздайте MCP-запись с дополнительными `-e`:
   ```powershell
   claude mcp remove atlassian -s user
   claude mcp add atlassian --scope user `
     -e 'CONFLUENCE_URL=${CONFLUENCE_URL}' `
     -e 'CONFLUENCE_PERSONAL_TOKEN=${CONFLUENCE_PERSONAL_TOKEN}' `
     -e 'JIRA_URL=${JIRA_URL}' `
     -e 'JIRA_PERSONAL_TOKEN=${JIRA_PERSONAL_TOKEN}' `
     -- "C:\Users\<вы>\AppData\Roaming\Python\Python3XX\Scripts\mcp-atlassian.exe"
   ```
4. Перезапустите Claude Code

---

## Troubleshooting

### `claude mcp list` показывает `✓ Connected`, но запросы возвращают пусто

«Connected» означает только, что MCP-процесс запустился — не то, что аутентификация в Confluence прошла. Проверьте токен прямым REST-запросом (способ C выше). Если ответ `"type":"anonymous"` → токен невалиден.

**Частые причины:**
- Скопировали не весь токен (длина < 44)
- Токен отозван или истёк
- В Windows User ENV записалось не то, что вы вставили (например, имя токена вместо значения)

**Решение:** создайте новый PAT, перепишите Windows User ENV, перезапустите Claude Code.

### Вы видите страницу в браузере, но API/MCP отвечает «no permission»

В Confluence DC бывает, что выпущенный ранее PAT не отражает права, выданные позже. Решение — **пересоздать токен**: отзовите старый, создайте новый, обновите Windows User ENV. Это решает 9 случаев из 10 «вижу-в-браузере-но-API-не-видит».

### Команда `pip install --user mcp-atlassian` падает с ошибкой

Проверьте, что Python вообще установлен и доступен:
```powershell
python --version
pip --version
```
Если нет — поставьте через `winget install Python.Python.3.12` или с python.org. После установки **перезайдите** в PowerShell, чтобы обновился PATH.

### Не нашли путь к `mcp-atlassian.exe`

Если `python -c "import sysconfig; ..."` показывает путь, в котором `.exe` нет — возможно, pip установил его в **глобальный** Python (без `--user`). Тогда путь будет вроде `C:\Program Files\Python3XX\Scripts\`. Универсальный способ найти:
```powershell
Get-Command mcp-atlassian -ErrorAction SilentlyContinue
```
Если команда видна — используйте её `Source` как путь в шаге 4.

### Перезапустил Claude Code, но MCP всё равно с старыми данными

VSCode иногда не убивает дочерние процессы при reload window. Попробуйте:
1. Закрыть VSCode полностью (через `File → Exit`, не крестик)
2. В `Task Manager` убить все процессы `claude*`, `node*`, `mcp-atlassian*` если остались
3. Открыть VSCode заново

---

## А что насчёт `.env`-файлов?

Короткий ответ: **`.env` ок как временный мост, но не как постоянное хранилище.**

В нашем сценарии (быстрый путь выше) `.env` живёт ровно столько, сколько нужно Claude Code, чтобы прочитать токен и перенести его в Windows User ENV. После миграции `.env` удаляется. Это удобный UX: пользователь не вставляет токен в чат и не учит синтаксис `[Environment]::SetEnvironmentVariable(...)`.

Почему **не** оставлять `.env` навсегда:

- Подстановка `${CONFLUENCE_PERSONAL_TOKEN}` в MCP-конфигах (`.mcp.json` / `.claude.json`) читает значение **из переменных окружения процесса**, не из `.env`. То есть MCP-сервер сам про `.env` не знает — кто-то должен явно его подгружать. Это делает Claude Code на этапе настройки, один раз.
- `.env` в папке проекта — это файл на диске. `gitignore` **не защищает** от `git add -f`, бэкапов в облако, копирования папки на флешку или зипования всего проекта.
- Дублирование секрета (`.env` + Windows User ENV) усложняет ротацию: легко обновить одно место и забыть другое, а через месяц гадать, почему ничего не работает.

`.env`-файлы — нормальный паттерн для **самих приложений**, которые явно читают их в коде (`python-dotenv`, `dotenv` npm-пакет, `docker compose env_file:`). Это отдельный сценарий, не наш.

**Долгосрочное хранилище токена:**
- Windows: `[Environment]::SetEnvironmentVariable(..., 'User')` — то есть Windows User ENV
- macOS/Linux: `export CONFLUENCE_PERSONAL_TOKEN=...` в `~/.zshrc` / `~/.bashrc` / `~/.profile`
- **Backup** в обоих случаях — менеджер паролей (Bitwarden / 1Password / KeePassXC)

---

## Безопасность

- **Не коммитьте токен в git**, даже в `.env`-файлах (gitignore не защищает от случайного `git add -f` или копирования папки в облако)
- **Не вставляйте токен в чат/Slack/email/мессенджеры** — он засветится в истории и логах
- Если случайно засветили — **немедленно отзовите** старый и создайте новый
- Единственное место хранения копии токена — **менеджер паролей**
- При уходе из компании / смене ноутбука — отзовите все свои PAT в Confluence

---

## Полезные ссылки

- Что такое Personal Access Tokens: https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html
- Документация MCP-сервера mcp-atlassian: https://github.com/sooperset/mcp-atlassian
- Claude Code (CLI): https://docs.claude.com/en/docs/claude-code
