# Worker Attendance Machines

Tool CLI untuk manajemen data mesin absensi ZKTeco. Menarik log punch dari mesin, menyimpannya secara lokal di SQLite, lalu mendorongnya ke CMS pusat (`deneire-cms`). Dilengkapi juga dengan peringatan kapasitas perangkat.

## Prasyarat

- **Python 3.14** (lihat `.python-version`, saat ini `3.14.6`)
- Akses jaringan LAN ke mesin ZKTeco (port default `4370`)
- **deneire-cms** sudah deploy endpoint `/iclock/employees` dan `/iclock/cdata`

## Instalasi

```bash
uv sync
cp config.example.json config.json
# Edit config.json sesuai environment Anda
```

## Konfigurasi

Salin `config.example.json` menjadi `config.json`, lalu sesuaikan nilai-nilainya:

```json
{
  "cms_base_url": "https://cms.example.com",
  "db_path": "attendance.db",
  "capacity_warning_pct": 90,
  "machines": [
    {
      "name": "Mesin Lantai 1",
      "ip": "192.168.1.100",
      "port": 4370,
      "serial_number": "SN001"
    }
  ]
}
```

### Penjelasan field

| Field | Wajib | Keterangan |
|---|---|---|
| `cms_base_url` | Ya | Base URL deneire-cms (contoh: `https://hris.perusahaan.com`) |
| `db_path` | Ya | Path file database SQLite lokal untuk menyimpan log sementara |
| `capacity_warning_pct` | Tidak | Persentase batas peringatan kapasitas perangkat (default: `90`). Jika jumlah log mencapai persentase ini, akan muncul warning |
| `machines` | Ya | Array daftar mesin. Setiap entry punya: |
| &nbsp;&nbsp;`name` | Ya | Nama label mesin (bebas, untuk identifikasi) |
| &nbsp;&nbsp;`ip` | Ya | Alamat IP mesin ZKTeco di jaringan LAN |
| &nbsp;&nbsp;`port` | Ya | Port layanan mesin ZKTeco (biasanya `4370`) |
| &nbsp;&nbsp;`serial_number` | Ya | **Serial number mesin — HARUS SAMA persis** dengan yang didaftarkan di CMS UI. Tanpa kecocokan ini, push ke CMS akan ditolak |

## Catatan Penting

Sebelum menjalankan `fetch` atau `sync-users`:

1. **Daftarkan mesin di CMS** — Buka CMS UI → `/dashboard/hris/attendance/machines`, daftarkan mesin baru dengan serial number yang sama dengan `serial_number` di `config.json`.
2. **Deploy endpoint `/iclock/employees`** — Endpoint ini wajib ada agar `sync-users` bisa mengambil daftar karyawan dari CMS. Tanpa ini, perintah sync-users akan gagal.

## Cara Pakai

Semua perintah dijalankan melalui `uv run agent.py <command>`.

### `fetch` — Tarik log dari mesin

Menarik semua log punch dari mesin ZKTeco, menyimpannya di database lokal, lalu mendorongnya ke CMS.

```bash
# Fetch semua mesin
uv run agent.py fetch

# Fetch satu mesin tertentu
uv run agent.py fetch --machine "Mesin Lantai 1"
```

Alur:
1. Pull logs dari mesin via protokol ZKTeco
2. Simpan ke SQLite lokal (dedup otomatis)
3. Push log yang belum sinkron ke CMS
4. Cek kapasitas perangkat dan tampilkan warning jika mendekati batas
5. Catat hasil fetch (sukses/gagal)

### `export` — Export data ke CSV

Mengexport log kehadiran dari database lokal ke file CSV dalam rentang tanggal tertentu.

```bash
uv run agent.py export \
  --machine "Mesin Lantai 1" \
  --from 2025-01-01 \
  --to 2025-01-31 \
  --out laporan_januari.csv
```

Kosongkan `--machine` untuk export semua mesin sekaligus ke satu file CSV, dan kosongkan `--from`/`--to` untuk export semua tanggal (tanpa batasan rentang):

```bash
uv run agent.py export --out laporan_semua.csv
```

Output CSV memiliki kolom: `machine`, `finger_id`, `punch_time`, `status`, `keterangan`.

`status` adalah kode punch mentah dari mesin (0/4 = masuk, 1/5 = keluar, 2 = istirahat keluar, 3 = istirahat masuk); `keterangan` adalah label yang sudah diterjemahkan (mis. "Masuk", "Keluar").

### `delete` — Hapus log di mesin

Menghapus semua log attendance di mesin ZKTeco. **Dilindungi**: menolak menghapus jika masih ada log yang belum disinkronkan ke CMS. Gunakan `--force` untuk memaksa penghapusan.

```bash
# Aman — ditolak jika ada unsynced logs
uv run agent.py delete --machine "Mesin Lantai 1"

# Paksa hapus walau ada unsynced logs
uv run agent.py delete --machine "Mesin Lantai 1" --force
```

### `status` — Ringkasan status mesin

Menampilkan tabel status semua mesin atau mesin tertentu: keterjangkauan, kapasitas, log belum sinkron, dan riwayat fetch.

```bash
# Status semua mesin
uv run agent.py status

# Status satu mesin
uv run agent.py status --machine "Mesin Lantai 1"
```

### `sync-users` — Sinkronisasi karyawan

Mengambil daftar karyawan dari CMS dan mendaftarkannya ke mesin ZKTeco (biometrik fingerprint provisioning).

```bash
uv run agent.py sync-users --machine "Mesin Lantai 1"
```

### `scan` — Cari mesin ZKTeco di jaringan

Scan subnet `/24` lokal (auto-detect dari IP komputer yang menjalankan tool, atau isi manual via `--subnet`) untuk mencari mesin ZKTeco yang menyala di jaringan — port default `4370`. Berguna kalau IP mesin belum diketahui atau berubah (misal karena DHCP), tanpa harus cek manual satu-satu.

```bash
# Scan subnet lokal (auto-detect), port default 4370
uv run agent.py scan

# Scan subnet tertentu / port custom
uv run agent.py scan --subnet 192.168.1 --port 4370
```

Contoh output:

```
----------------------------------------------------------------------------------
IP               Port   Serial               Device Name          Status
----------------------------------------------------------------------------------
192.168.1.100    4370   SN001                ZK-XXXX              Terdaftar (Mesin Lantai 1)
192.168.1.150    4370   SN003                ZK-XXXX              Belum terdaftar
----------------------------------------------------------------------------------
```

**Catatan**: `scan` hanya *menampilkan* hasil (IP, serial number, dan status apakah sudah terdaftar/cocok dengan `config.json`) — tidak otomatis mengubah `config.json`. Kalau ada mesin baru atau IP berubah, tetap edit `config.json` manual berdasarkan hasil scan ini. Scan cuma menjangkau device yang satu subnet `/24` dengan komputer yang menjalankan tool; kalau mesin ada di subnet lain, pakai `--subnet`.

### `update-time` — Sinkronkan waktu mesin

Mengatur jam mesin ZKTeco mengikuti waktu lokal komputer yang menjalankan agent.
Tanpa `--machine`, seluruh mesin di `config.json` akan diperbarui.

```bash
# Update semua mesin
uv run agent.py update-time

# Update satu mesin
uv run agent.py update-time --machine "Mesin Lantai 1"
```

Mesin menyimpan tanggal dan jam sebagai wall-clock tanpa informasi timezone,
jadi pastikan timezone komputer sudah benar sebelum menjalankan perintah ini.

## Crontab — Fetch Otomatis

Tambahkan baris berikut ke crontab (`crontab -e`) untuk menarik log setiap 5 menit:

```cron
*/5 * * * * cd /path/to/worker-attendance-machines && uv run agent.py fetch >> agent.log 2>&1
```

Atau jika ingin hanya mesin tertentu:

```cron
*/5 * * * * cd /path/to/worker-attendance-machines && uv run agent.py fetch --machine "Mesin Lantai 1" >> agent.log 2>&1
```

## Build & Jalankan di Windows

Kode ini sudah cross-platform (tidak ada dependency khusus Linux), jadi bisa
dijalankan langsung di Windows lewat `uv run agent.py <command>`. Untuk mesin
Windows yang tidak mau install Python/uv, package jadi `.exe` standalone
pakai [PyInstaller](https://pyinstaller.org/).

### Opsi A — Download dari GitHub Release (paling gampang)

Repo ini punya workflow yang otomatis build `.exe` di runner Windows tiap
push ke `main` dan mempublikasikannya ke release `latest` (lihat
`.github/workflows/build-windows.yml`). Download langsung:

**[⬇ Download attendance-agent-windows.zip](https://github.com/harizinside/worker-attendance-machines/releases/latest/download/attendance-agent-windows.zip)**

Link ini selalu mengarah ke build terbaru dari `main`. Riwayat build lain
bisa dilihat di tab **Actions** atau **Releases**.

### Opsi B — Build manual di mesin Windows

PyInstaller tidak bisa cross-compile, jadi build harus dilakukan di mesin
Windows (bukan dari Mac/Linux).

Prasyarat: Python 3.14 + [uv](https://docs.astral.sh/uv/) terinstall.

```powershell
uv sync --group dev
uv run pyinstaller --onedir --name attendance-agent agent.py
Copy-Item config.example.json dist\attendance-agent\config.example.json
Compress-Archive -Path dist\attendance-agent -DestinationPath dist\attendance-agent-windows.zip
```

Hasilnya ada di `dist\attendance-agent-windows.zip`.

> Kalau versi PyInstaller yang ter-install belum support Python 3.14 (rilis
> masih baru), build pakai Python 3.13 di venv Windows sebagai fallback.

### Cara pakai `.exe`

Extract `attendance-agent-windows.zip` seluruhnya. Jangan memindahkan
`attendance-agent.exe` keluar dari folder hasil extract karena folder
`_internal` berisi DLL dan modul Python yang dibutuhkan aplikasi.

Build menggunakan mode PyInstaller `--onedir`, bukan `--onefile`. Dengan begitu
DLL seperti `_sqlite3.pyd` dimuat dari folder aplikasi dan tidak diekstrak ke
`%TEMP%\_MEI...`, yang dapat diblokir Windows Application Control pada komputer
kantor.

`config.json`/`db_path` di-resolve relatif ke *working directory* saat exe
dijalankan (sama seperti versi `uv run`). Rename `config.example.json` menjadi
`config.json`, lalu edit konfigurasinya. Struktur foldernya seperti ini:

```
C:\AttendanceAgent\
└── attendance-agent\
    ├── attendance-agent.exe
    ├── config.example.json
    ├── config.json          ← rename/copy example, isi sesuai environment
    ├── attendance.db        ← dibuat otomatis saat pertama kali fetch/status
    └── _internal\           ← wajib tetap bersama exe
```

Edit `config.json` sesuai environment (lihat bagian
[Konfigurasi](#konfigurasi) di atas), lalu jalankan dari `cmd`/PowerShell:

```powershell
attendance-agent.exe status
attendance-agent.exe fetch
attendance-agent.exe export --machine "Mesin Lantai 1" --from 2025-01-01 --to 2025-01-31 --out laporan.csv
```

### File log untuk laporan error

Setiap kali aplikasi dijalankan, output diagnostik dan crash yang tidak
tertangani otomatis ditulis ke `attendance-agent.log`. Pada Windows file ini
normalnya berada di folder yang sama dengan `attendance-agent.exe`, jadi cukup
kirim file tersebut saat melaporkan error.

Jika folder exe tidak bisa ditulis (misalnya berada di `Program Files`), log
disimpan di `%LOCALAPPDATA%\AttendanceAgent\attendance-agent.log`. Log dibatasi
5 MB dan maksimal tiga file lama (`.log.1` sampai `.log.3`) agar disk tidak
penuh. Informasi versi Python, Windows, working directory, dan traceback crash
ikut dicatat; `config.json` dan isi database tidak disalin ke log.

### Task Scheduler — pengganti crontab

1. Buka **Task Scheduler** → **Create Basic Task**.
2. Trigger: **Daily**, recur every 1 day, lalu edit trigger jadi repeat
   every 5 minutes (atau set langsung di trigger advanced settings).
3. Action: **Start a program** →
   - Program/script: path lengkap ke `attendance-agent.exe`
   - Add arguments: `fetch`
   - **Start in**: folder yang berisi `config.json` — **wajib diisi**,
     kalau kosong Task Scheduler tidak bisa menemukan `config.json`.
4. Simpan, lalu cek tab **History** setelah beberapa siklus untuk pastikan
   tereksekusi otomatis.

### Keamanan `config.json`

`config.json` berisi detail jaringan internal (IP mesin, URL CMS) dalam
bentuk plaintext — file ini **tidak**
ikut ter-bundle ke dalam `.exe` (PyInstaller cuma bundle kode), jadi aman
untuk di-share/copy `.exe`-nya ke mesin lain. Tapi tetap perhatikan:

- Jangan taruh `config.json` di folder yang di-sync ke cloud (OneDrive/
  Dropbox) atau network share yang bisa diakses banyak orang.
- File ini sudah masuk `.gitignore` — jangan pernah commit `config.json`
  asli ke git.
- Opsional: restrict akses folder cuma untuk akun yang menjalankan Task
  Scheduler lewat `icacls "C:\path\to\folder" /inheritance:r /grant:r "%USERNAME%:F"`.

## Cara Kerja

### Alur `fetch`

```
Mesin ZKTeco ──pull_logs──> agent.py
                               │
                    ┌──────────┴──────────┐
                  sukses                gagal
                    │                     │
            simpan ke SQLite      catat fetch gagal
                    │
              push ke CMS
                    │
              mark_pushed
                    │
              cek kapasitas
```

### Guard `delete`

Perintah `delete` memeriksa apakah ada log yang belum disinkronkan (`pushed_to_cms = 0`). Jika ada, operasi ditolak kecuali flag `--force` diberikan. Ini mencegah kehilangan data yang belum terbackup ke CMS.

## Troubleshooting

### Mesin tidak reachable

- Pastikan mesin ZKTeco terhubung ke jaringan LAN yang sama dengan server
- Verifikasi IP dan port di `config.json` benar
- Test koneksi manual: `nc -zv <IP_MESIN> 4370`
- Periksa firewall yang mungkin memblokir port 4370

### CMS tidak respond

- Verifikasi endpoint CMS bisa diakses: `curl https://cms.example.com/iclock/cdata?SN=SN001&table=ATTLOG`
- Periksa koneksi jaringan antara server dan CMS
- Lihat log aplikasi (`agent.log`) untuk detail error HTTP
- Pastikan `cms_base_url` di config tidak memiliki trailing slash

### Database corrupt

- Backup database terlebih dahulu: `cp attendance.db attendance.db.bak`
- Tool ini menggunakan WAL mode untuk mengurangi risiko korupsi data
- Jika terjadi masalah, hapus file `.db-wal` dan `.db-shm` (SQLite akan rebuild otomatis)
