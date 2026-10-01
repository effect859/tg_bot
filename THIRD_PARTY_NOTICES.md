# Сторонние компоненты

Перечень сторонних библиотек, которые попадают в рабочую сборку бота вместе с
бинарными компонентами внутри колёс Python, и обязательства по их лицензиям.
Документ — техническая инвентаризация, а не юридическое заключение: оценку модели
поставки (работа только на собственном сервере или передача образа и кода третьим
лицам) должен дать юрист.

Проверено для сборки на Linux x86_64 в образе `python:3.12-slim` (Debian, glibc):
колёса manylinux. Колесо pypdfium2 для musl (Alpine) — другая сборка с иным
составом лицензий (в том числе `libgcc_s` и `libstdc++`), поэтому Alpine не
используется. Состав колёс для других платформ отдельно не проверялся.

## Извлечение текста (Stage 2.4)

| Пакет | Версия | Колесо (SHA-256) |
|---|---|---|
| pypdfium2 | 5.13.0 | `pypdfium2-5.13.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl` (`81df25c1ab4c13ff773102d3cbea1967511d079123b067fc077bd0c4d57d91d8`) |
| lxml | 6.1.3 | `lxml-6.1.3-cp312-cp312-manylinux_2_26_x86_64.manylinux_2_28_x86_64.whl` (`909f4e927bb051f7740d6367285fc60cdcfdaf0258c2dba4ff5ba7eadadc250c`) |

Суммы совпадают с опубликованными на PyPI (проверено 2026-09-30 и повторно
2026-10-01 при скачивании в контейнере).

### pypdfium2 5.13.0

- Сам пакет: Apache-2.0 или BSD-3-Clause на выбор. Документация и примеры
  pypdfium2 — CC-BY-4.0: код из них не копируется без указания авторства.
- Встроенный `libpdfium.so` — PDFium 153.0.7999.0 в сборке проекта
  pdfium-binaries (MIT, © Benoit Blanchon).
- Лицензии компонентов сборки, как они поставляются в колесе
  (`pypdfium2-5.13.0.dist-info/licenses/data/linux_x64/BUILD_LICENSES/`):

| Компонент | Лицензия |
|---|---|
| PDFium | BSD-3-Clause (© The PDFium Authors) и лицензии сторонних частей в том же файле |
| pdfium-binaries | MIT |
| Abseil | Apache-2.0 |
| Anti-Grain Geometry 2.3 | разрешительная лицензия AGG |
| fast_float | MIT |
| FreeType | FreeType License (FTL) |
| ICU | Unicode License v3 |
| Little CMS | MIT |
| libjpeg-turbo | IJG License и BSD-3-Clause |
| OpenJPEG | BSD-2-Clause |
| libpng | PNG Reference Library License v2 |
| libtiff | лицензия libtiff (в стиле BSD) |
| LLVM libc | Apache-2.0 with LLVM Exceptions |
| simdutf | MIT |
| zlib | zlib License |

Обязательные упоминания:

- Portions of this software are copyright © The FreeType Project
  (www.freetype.org). All rights reserved.
- This software is based in part on the work of the Independent JPEG Group.

### lxml 6.1.3

- Сам пакет: BSD-3-Clause; части кода — PSF-2.0 и ElementTree License (см.
  `lxml-6.1.3.dist-info/licenses/LICENSES.txt`).
- Модули lxml статически собраны с библиотеками, **лицензии которых в колесо не
  вложены** — их тексты нужно приложить из исходных проектов:

| Библиотека | Версия в колесе | Лицензия |
|---|---|---|
| libxml2 | 2.14.6 | MIT |
| libxslt / libexslt | 1.1.43 | MIT |
| zlib | 1.3.2 | zlib License |
| GNU libiconv | не установлена (в модуле есть символ `_libiconv_version`) | **LGPL-2.1-or-later** |

**GNU libiconv под LGPL статически встроена в `lxml/etree.*.so`.** При
распространении бинарных файлов (например, передаче Docker-образа третьим лицам)
LGPL требует, в частности, предоставить исходный код библиотеки и возможность
пересобрать модуль с изменённой версией. При работе бота только на собственном
сервере распространения, вероятно, не происходит. Решение по модели поставки —
за юристом.

## Действия перед коммерческим запуском

1. Собирать образ на `python:3.12-slim` (glibc), не на Alpine.
2. Устанавливать зависимости с проверкой хешей (`pip install --require-hashes`) и
   не удалять `*.dist-info/licenses/` при сборке образа.
3. Приложить к поставке тексты лицензий из колёс и лицензии libxml2, libxslt,
   zlib и GNU libiconv из исходных проектов.
4. Получить оценку юриста по модели поставки, в том числе по LGPL для libiconv.
5. Следить за выпусками pypdfium2 (встроенный PDFium отстаёт от актуального) и
   lxml; после каждого обновления повторять тесты извлечения в Linux-контейнере
   и обновлять этот файл.
