import ctypes
from ctypes import wintypes
import os
import time
import subprocess

# === НАСТРОЙКИ ===
OUTPUT_DIR = "damages"
SLOW_THRESHOLD_MS = 150       # Порог задержки в мс (аналог оранжевых блоков Victoria)
SECTORS_PER_BLOCK = 128       # Сколько секторов читать за один проход

# Константы Win32 API для прямого доступа к диску без кэширования
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_BEGIN = 0
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def list_physical_drives():
    """Выводит список физических дисков в Windows через PowerShell."""
    print("=== Доступные физические диски ===")
    try:
        cmd = 'powershell -Command "Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, @{Name=\'SizeGB\';Expression={[math]::Round($_.Size/1GB, 2)}} | Format-Table -AutoSize"'
        subprocess.run(cmd, shell=True)
    except Exception:
        print("Не удалось получить список автоматически. Посмотрите номера в 'Управление дисками'.")


def open_raw_disk(drive_number):
    """Открывает физический диск напрямую через Win32 API без системного кэша."""
    path = f"\\\\.\\PhysicalDrive{drive_number}"
    handle = kernel32.CreateFileW(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_NO_BUFFERING,
        None
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        raise PermissionError(f"Не удалось открыть {path}. Код ошибки Win32: {err} (Запустите от имени Администратора!)")
    return handle


def set_disk_pointer(handle, offset):
    """Перемещает указатель чтения на заданное смещение (в байтах)."""
    new_pos = ctypes.c_int64()
    res = kernel32.SetFilePointerEx(handle, ctypes.c_int64(offset), ctypes.byref(new_pos), FILE_BEGIN)
    if not res:
        raise OSError(f"Ошибка позиционирования SetFilePointerEx: {ctypes.get_last_error()}")


def read_raw_bytes(handle, size):
    """Читает точное количество байт напрямую с контроллера диска."""
    buffer = ctypes.create_string_buffer(size)
    bytes_read = wintypes.DWORD(0)
    res = kernel32.ReadFile(handle, buffer, size, ctypes.byref(bytes_read), None)
    if not res:
        err = ctypes.get_last_error()
        raise OSError(f"Ошибка чтения ReadFile (Win32 Error {err})")
    return buffer.raw[:bytes_read.value]


def scan_damaged_block(handle, start_offset, block_size, sector_size, log_file):
    """Посекторное вычитывание повреждённого блока и сохранение в папку damages."""
    bad_count = 0
    recovered_data = bytearray()
    start_sector = start_offset // sector_size

    for s_offset in range(0, block_size, sector_size):
        pos = start_offset + s_offset
        sector_num = pos // sector_size
        try:
            set_disk_pointer(handle, pos)
            data = read_raw_bytes(handle, sector_size)
            if not data:
                break
            recovered_data.extend(data)
        except OSError:
            bad_count += 1
            print(f"  [!] БИТЫЙ СЕКТОР (ERR): #{sector_num} (смещение {pos} байт)")
            log_file.write(f"{sector_num}\n")
            # Заполняем нечитаемый сектор нулями для сохранения структуры дампа
            recovered_data.extend(b"\x00" * sector_size)

    dump_filename = os.path.join(OUTPUT_DIR, f"damaged_block_LBA_{start_sector}.bin")
    with open(dump_filename, "wb") as f:
        f.write(recovered_data)

    return bad_count


def main():
    list_physical_drives()
    drive_num = input("Введите номер диска для проверки (например, 0 или 1): ").strip()
    if not drive_num.isdigit():
        print("Некорректный номер диска.")
        return

    sector_size = 512
    block_size = sector_size * SECTORS_PER_BLOCK

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log_path = os.path.join(OUTPUT_DIR, f"bad_sectors_drive{drive_num}.txt")

    try:
        handle = open_raw_disk(drive_num)
    except PermissionError as e:
        print(f"\nОШИБКА: {e}")
        return

    print(f"\nНачато прямое сканирование PhysicalDrive{drive_num} (без кэша Windows)...")
    print(f"Дампы сбойных участков будут сохранены в: {os.path.abspath(OUTPUT_DIR)}\n")

    offset = 0
    slow_blocks = 0
    bad_sectors = 0
    start_scan_time = time.time()

    try:
        with open(log_path, "w") as log_file:
            while True:
                t0 = time.perf_counter()
                try:
                    set_disk_pointer(handle, offset)
                    data = read_raw_bytes(handle, block_size)
                    elapsed_ms = (time.perf_counter() - t0) * 1000

                    if not data:
                        print("\nСканирование успешно завершено (достигнут конец диска).")
                        break

                    # Фиксация медленных блоков (деградация поверхности / контроллера)
                    if elapsed_ms > SLOW_THRESHOLD_MS:
                        slow_blocks += 1
                        lba = offset // sector_size
                        print(f"[МЕДЛЕННО] LBA {lba} | Отклик: {elapsed_ms:.1f} мс")
                        slow_file = os.path.join(OUTPUT_DIR, f"slow_LBA_{lba}_{int(elapsed_ms)}ms.bin")
                        with open(slow_file, "wb") as sf:
                            sf.write(data)

                except OSError:
                    print(f"\n[СБОЙ ЧТЕНИЯ] Смещение {offset}. Переход на посекторный анализ...")
                    bad_in_block = scan_damaged_block(handle, offset, block_size, sector_size, log_file)
                    bad_sectors += bad_in_block

                offset += block_size

                # Статистика каждые 1 ГБ
                if offset % (1024 ** 3) == 0:
                    gb = offset / (1024 ** 3)
                    speed = gb / max(0.1, (time.time() - start_scan_time)) * 1024
                    print(f"Проверено: {gb:.0f} ГБ | Скорость: {speed:.1f} МБ/с | Медленных: {slow_blocks} | Битых: {bad_sectors}")

    except KeyboardInterrupt:
        print("\nПроверка остановлена пользователем.")
    finally:
        kernel32.CloseHandle(handle)

    print("\n=== ИТОГИ ПРОВЕРКИ ===")
    print(f"Всего проверено: {offset / (1024 ** 3):.2f} ГБ")
    print(f"Медленных участков (>{SLOW_THRESHOLD_MS} мс): {slow_blocks}")
    print(f"Битых (нечитаемых) секторов: {bad_sectors}")
    print(f"Лог и дампы сохранены в папку: {os.path.abspath(OUTPUT_DIR)}")


if __name__ == "__main__":
    main()
