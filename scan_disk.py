
python
import os
import time

# === НАСТРОЙКИ ===
# Для Windows: r"\\.\PhysicalDrive0" (0, 1, 2... - номер диска в Управлении дисками)
# Для Linux: "/dev/sda"
DEVICE_PATH = r"\\.\PhysicalDrive1"  # УКАЖИТЕ ВАШ ДИСК!

BLOCK_SIZE = 512 * 128  # Читаем блоками по 64 КБ (128 секторов) для скорости
SECTOR_SIZE = 512       # Стандартный размер сектора
SLOW_THRESHOLD_MS = 200 # Порог "медленного" сектора в миллисекундах (оранжевый в Victoria)
OUTPUT_DIR = "damages"  # Папка для сохранения дампов проблемных участков


def scan_disk():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    bad_sectors_log = os.path.join(OUTPUT_DIR, "bad_sectors_list.txt")

    print(f"Запуск сканирования: {DEVICE_PATH}")
    print(f"Блок чтения: {BLOCK_SIZE} байт | Порог задержки: {SLOW_THRESHOLD_MS} мс\n")

    offset = 0
    total_bad = 0
    total_slow = 0

    try:
        with open(DEVICE_PATH, "rb") as disk, open(bad_sectors_log, "w") as log_file:
            while True:
                start_time = time.perf_counter()
                try:
                    disk.seek(offset)
                    data = disk.read(BLOCK_SIZE)
                    elapsed_ms = (time.perf_counter() - start_time) * 1000

                    if not data:
                        print("\nДостигнут конец диска.")
                        break

                    # Проверка на медленный отклик (деградирующий участок)
                    if elapsed_ms > SLOW_THRESHOLD_MS:
                        total_slow += 1
                        sector_num = offset // SECTOR_SIZE
                        print(f"[МЕДЛЕННО] Сектор {sector_num} | Задержка: {elapsed_ms:.1f} мс")
                        
                        # Сохраняем дамп медленного участка, пока он еще читается
                        dump_name = os.path.join(OUTPUT_DIR, f"slow_sector_{sector_num}.bin")
                        with open(dump_name, "wb") as f_dump:
                            f_dump.write(data)

                except (OSError, IOError) as e:
                    # Ошибка чтения блока -> переходим на посекторное сканирование (по 512 байт)
                    print(f"\n[ОШИБКА БЛОКА] Смещение {offset}. Переход на посекторный анализ...")
                    bad_in_block = scan_block_by_sector(disk, offset, BLOCK_SIZE, log_file)
                    total_bad += bad_in_block

                offset += BLOCK_SIZE

                # Вывод прогресса каждые 500 МБ
                if offset % (500 * 1024 * 1024) == 0:
                    gb_scanned = offset / (1024 ** 3)
                    print(f"Проверено: {gb_scanned:.2f} ГБ | Медленных: {total_slow} | Битых секторов: {total_bad}")

    except PermissionError:
        print("ОШИБКА: Запустите скрипт от имени Администратора (или через sudo)!")
    except FileNotFoundError:
        print(f"ОШИБКА: Устройство {DEVICE_PATH} не найдено. Проверьте номер диска.")
    except KeyboardInterrupt:
        print("\nСканирование прервано пользователем.")

    print(f"\nГотово! Медленных блоков: {total_slow}, Битых секторов: {total_bad}")
    print(f"Результаты сохранены в папку: {os.path.abspath(OUTPUT_DIR)}")


def scan_block_by_sector(disk, start_offset, block_size, log_file):
    """Точное посекторное чтение проблемного блока для спасения уцелевших данных."""
    bad_count = 0
    recovered_buffer = bytearray()
    block_start_sector = start_offset // SECTOR_SIZE

    for s_offset in range(0, block_size, SECTOR_SIZE):
        current_pos = start_offset + s_offset
        sector_num = current_pos // SECTOR_SIZE
        try:
            disk.seek(current_pos)
            sector_data = disk.read(SECTOR_SIZE)
            recovered_buffer.extend(sector_data)
        except (OSError, IOError):
            bad_count += 1
            print(f"  -> [BAD SECTOR] Физический сектор #{sector_num} не читается!")
            log_file.write(f"{sector_num}\n")
            # Заполняем нечитаемый сектор нулями, чтобы сохранить структуру дампа
            recovered_buffer.extend(b"\x00" * SECTOR_SIZE)

    # Сохраняем частично спасенный блок в папку damages
    dump_path = os.path.join(OUTPUT_DIR, f"damaged_block_sector_{block_start_sector}.bin")
    with open(dump_path, "wb") as f:
        f.write(recovered_buffer)

    return bad_count


if __name__ == "__main__":
    scan_disk()
Use code with caution.
