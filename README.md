# Obxod-BOX

**Obxod-BOX** — это клиент для обхода блокировок с графическим интерфейсом, построенный на Python и Toga. Приложение управляет Xray-ядром, предоставляет системный трей, настройки маршрутизации и удобный установщик для Windows.

## 🚀 Возможности

- **Графический интерфейс** на Toga (кросс-платформенный)
- **Системный трей** с быстрым доступом к настройкам
- **Управление Xray** — автоматический запуск/остановка ядра
- **Маршрутизация** — пресеты для обхода блокировок (RU-auto-routing и др.)
- **Настройки провайдеров** — гибкая конфигурация подключений
- **Установщик для Windows** — SFX-архив с 7-Zip (включает Python runtime)
- **Кроссплатформенность** — исходники упакованы для запуска на Linux/macOS

## 📦 Структура проекта

```
obxod-box/
├── main.py                 # Точка входа
├── main_window.py          # Главное окно приложения
├── config.py               # Конфигурация
├── net_utils.py            # Сетевые утилиты
├── network_watcher.py      # Мониторинг сети
├── platform_info.py        # Информация о платформе
├── platform_tray.py        # Системный трей
├── xray_manager.py         # Управление Xray
├── xray_supervisor.py      # Супервизор Xray
├── loading_window.py       # Окно загрузки
├── start.bat / start.sh    # Скрипты запуска
├── requirements.txt        # Python зависимости
├── assets/                 # Ресурсы (иконки, изображения)
├── installer/              # Модуль установщика (SFX)
│   ├── build_sfx_proper.py # Сборка SFX установщика
│   ├── core.py             # Ядро установщика
│   ├── gui.py              # GUI установщика
│   ├── cli.py              # CLI установщика
│   ├── install.bat         # Скрипт установки
│   └── 7zsd_All.sfx        # 7-Zip SFX модуль
├── runtime/                # Встроенный Python runtime (для Windows)
├── cfg/                    # Конфигурационные файлы
│   ├── routing/            # Пресеты маршрутизации
│   └── providers/          # Настройки провайдеров
├── mkbuild.ps1             # Скрипт сборки релизов (PowerShell)
├── .buildignore            # Исключения для сборки
└── .gitignore              # Исключения для Git
```

## 🛠 Сборка релизов

### Локальная сборка (Windows)

```powershell
# Сборка с автоматическим поднятием patch-версии (x)
.\mkbuild.ps1

# Сборка с поднятием minor-версии (y)
.\mkbuild.ps1 -Bump y

# Сборка с поднятием major-версии (z)
.\mkbuild.ps1 -Bump z

# Только бандл приложения (без установщика)
.\mkbuild.ps1 -NoInstaller
```

### Автоматическая сборка (GitHub Actions)

При пуше в `main` автоматически запускается workflow:
1. Устанавливает Python 3.11 и 7-Zip
2. Запускает `mkbuild.ps1` для сборки установщика и бандла
3. Создает GitHub Release с артефактами:
   - `Obxod-BOX-Setup-vX.Y.Z.exe` — установщик для Windows
   - `obxod-box-vX.Y.Z-windows.zip` — архив с установщиком и бандлом приложения

Артефакты доступны в разделе **Releases** репозитория.

## 🖥 Запуск из исходников

### Windows
```cmd
start.bat
```

### Linux / macOS
```bash
# Установить зависимости
pip install -r requirements.txt

# Запуск
python main.py
```

## ⚙️ Конфигурация

При первом запуске создаются:
- `cfg/app-config.json` — основные настройки приложения
- `cfg/x-ray_conf.json` — конфигурация Xray
- `cfg/routing/` — пресеты маршрутизации
- `cfg/providers/` — настройки провайдеров
- `bin/` — бинарные файлы Xray (скачиваются автоматически)
- `geo/` — geoip/geosite файлы (скачиваются автоматически)
- `logs/` — логи приложения

## 📋 Требования

### Для разработки
- Python 3.11+
- 7-Zip (для сборки установщика на Windows)

### Для запуска
- Windows 10/11 (установщик включает Python runtime)
- Linux/macOS — Python 3.11+ + зависимости из `requirements.txt`

## 🔧 Зависимости

```
toga                    # GUI фреймворк
psutil                  # Системные утилиты
pythonnet (Windows)     # .NET интеграция для трея
```

## 📄 Лицензия

MIT License — см. файл [LICENSE](LICENSE) для деталей.

## 🤝 Участие в разработке

1. Форкните репозиторий
2. Создайте ветку для фичи (`git checkout -b feature/amazing-feature`)
3. Закоммитьте изменения (`git commit -m 'Add amazing feature'`)
4. Запушьте ветку (`git push origin feature/amazing-feature`)
5. Откройте Pull Request

## 📞 Поддержка

- Issues: [GitHub Issues](https://github.com/ADSFAL-US/Obxod-Box/issues)
- Discussions: [GitHub Discussions](https://github.com/ADSFAL-US/Obxod-Box/discussions)