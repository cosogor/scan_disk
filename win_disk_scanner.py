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
### Что добавлено в этой версии:
### 1. **Визуальная карта поверхности (78×26 квадратов):** весь выбранный диапазон диска автоматически масштабируется под сетку. Если внутри одного квадрата встретился хоть один медленный или битый сектор, квадрат окрасится в **оранжевый** или **красный** цвет, чтобы вы не пропустили ни одной аномалии.
### 2. **Выбор диапазона в гигабайтах (`Старт ГБ` / `Конец ГБ`):** при выборе диска поля автоматически заполняются его полным объёмом, но вы можете указать любой отрезок (например, проверить только последние 50 ГБ или конкретную проблемную зону).
### 3. **Настройка порога задержки прямо в окне:** для старого HDD можно выставить `150–200` мс, а для современного NVMe/SSD — `30–50` мс.
### 4. **Автоматический дамп в `damages`:** при обнаружении медленных (`slow_LBA_...bin`) или нечитаемых (`damaged_LBA_...bin`) участков скрипт сразу сохраняет их содержимое и дописывает номера битых секторов в `bad_sectors_driveX.txt`.
### 
 

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


def _read_bytes(self, handle, size):
buf = ctypes.create_string_buffer(size)
read_cnt = wintypes.DWORD(0)
if not kernel32.ReadFile(handle, buf, size, ctypes.byref(read_cnt), None):
raise OSError(f"ReadFile error: {ctypes.get_last_error()}")
return buf.raw[:read_cnt.value]
def _scan_damaged_block(self, handle, start_offset, block_size, log_file):
bad_count = 0
recovered = bytearray()
start_lba = start_offset // SECTOR_SIZE
for s_off in range(0, block_size, SECTOR_SIZE):
if self.stop_event.is_set():
break
pos = start_offset + s_off
lba = pos // SECTOR_SIZE
try:
self._set_pointer(handle, pos)
data = self._read_bytes(handle, SECTOR_SIZE)
if not data:
break
recovered.extend(data)
except OSError:
bad_count += 1
self.log(f"  [ERR] Битый сектор LBA #{lba} (смещение {pos} байт)")
log_file.write(f"{lba}\n")
recovered.extend(b"\x00" * SECTOR_SIZE)
dump_file = os.path.join(OUTPUT_DIR, f"damaged_LBA_{start_lba}.bin")
with open(dump_file, "wb") as f:
f.write(recovered)
return bad_count
def _scan_worker(self, drive_id, start_offset, end_offset, slow_ms):
os.makedirs(OUTPUT_DIR, exist_ok=True)
log_path = os.path.join(OUTPUT_DIR, f"bad_sectors_drive{drive_id}.txt")
block_size = SECTOR_SIZE * SECTORS_PER_BLOCK
total_bytes = end_offset - start_offset
bytes_per_cell = max(block_size, total_bytes // self.max_cells)
try:
handle = self._open_disk(drive_id)
except PermissionError as e:
self.log(f"ОШИБКА: {e}")
self._finish_ui("Ошибка прав доступа")
return
self.log(f"Старт проверки PhysicalDrive{drive_id} | Диапазон: {start_offset/(10243):.2f} - {end_offset/(10243):.2f} ГБ")
offset = start_offset
slow_count = 0
bad_count = 0
t_start = time.time()
current_cell = 0
cell_worst_color = "#4caf50"
cell_bytes_acc = 0
try:
with open(log_path, "a") as log_file:
while offset < end_offset and not self.stop_event.is_set():
read_len = min(block_size, end_offset - offset)
# Выравнивание размера чтения по сектору
read_len = (read_len // SECTOR_SIZE) * SECTOR_SIZE
if read_len == 0:
break
t0 = time.perf_counter()
try:
self._set_pointer(handle, offset)
data = self._read_bytes(handle, read_len)
dt_ms = (time.perf_counter() - t0) * 1000
if not data:
break
if dt_ms > slow_ms:
slow_count += 1
lba = offset // SECTOR_SIZE
self.log(f"[МЕДЛЕННО] LBA #{lba} ({dt_ms:.1f} мс) -> сохранено в {OUTPUT_DIR}")
slow_path = os.path.join(OUTPUT_DIR, f"slow_LBA_{lba}_{int(dt_ms)}ms.bin")
with open(slow_path, "wb") as sf:
sf.write(data)
block_color = "#ff9800"
elif dt_ms > 80:
block_color = "#ffeb3b"
elif dt_ms > 30:
block_color = "#8bc34a"
else:
block_color = "#4caf50"
except OSError:
self.log(f"[СБОЙ] Ошибка чтения на смещении {offset}. Анализ по секторам...")
b_found = self._scan_damaged_block(handle, offset, read_len, log_file)
bad_count += b_found
block_color = "#f44336"
# Приоритет цвета для ячейки карты (красный > оранжевый > жёлтый > зелёный)
priority = {"#4caf50": 1, "#8bc34a": 2, "#ffeb3b": 3, "#ff9800": 4, "#f44336": 5}
if priority[block_color] > priority[cell_worst_color]:
cell_worst_color = block_color
cell_bytes_acc += read_len
if cell_bytes_acc >= bytes_per_cell:
self.draw_cell(current_cell, cell_worst_color)
current_cell += 1
cell_bytes_acc = 0
cell_worst_color = "#4caf50"
offset += read_len
# Обновление прогресса
scanned = offset - start_offset
pct = (scanned / total_bytes) * 100
elapsed = max(0.1, time.time() - t_start)
speed_mb = (scanned / (1024 ** 2)) / elapsed
self.progress_var.set(pct)
self.stats_text.set(
f"Проверено: {scanned/(10243):.2f} / {total_bytes/(10243):.2f} ГБ ({pct:.1f}%) | "
f"Скорость: {speed_mb:.1f} МБ/с | Медленных: {slow_count} | Битых: {bad_count}"
)
if cell_bytes_acc > 0 and current_cell < self.max_cells:
self.draw_cell(current_cell, cell_worst_color)
finally:
kernel32.CloseHandle(handle)
status = "Остановлено пользователем" if self.stop_event.is_set() else "Сканирование завершено!"
self.log(f"\n=== {status} Медленных: {slow_count}, Битых секторов: {bad_count} ===")
self._finish_ui(status)
def _finish_ui(self, status_text):
self.is_scanning = False
self.root.after(0, lambda: self.start_btn.configure(state="normal"))
self.root.after(0, lambda: self.stop_btn.configure(state="disabled"))
self.root.after(0, lambda: self.status_label.configure(text=status_text, foreground="black"))
if name == "main":
root = tk.Tk()
app = DiskScannerGUI(root)
root.mainloop()
