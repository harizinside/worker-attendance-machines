# Worker Attendance Machines

Tool CLI untuk manajemen data mesin absensi ZKTeco. Menarik log punch dari mesin, menyimpannya secara lokal di SQLite, lalu mendorongnya ke CMS pusat (`deneire-cms`). Dilengkapi juga dengan alert WhatsApp via WAHA saat mesin offline dan peringatan kapasitas perangkat.

## Prasyarat

- **Python 3.14** (lihat `.python-version`, saat ini `3.14.6`)
- Akses jaringan LAN ke mesin ZKTeco (port default `4370`)
- **deneire-cms** sudah deploy endpoint `/iclock/employees` dan `/iclock/cdata`
- (Opsional) **WAHA** (WhatsApp HTTP API) sudah berjalan untuk fitur alert

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
  "offline_alert_after_cycles": 3,
  "waha": {
    "api_url": "http://localhost:3001/api",
    "api_key": "",
    "session": "default",
    "alert_phone": "62812xxxxxxx"
  },
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
| `offline_alert_after_cycles` | Tidak | Jumlah siklus fetch berturut-turut gagal sebelum mengirim alert WA (default: `3`) |
| `waha.api_url` | Tidak | URL endpoint WAHA (contoh: `http://localhost:3001/api`) |
| `waha.api_key` | Tidak | API key WAHA jika diperlukan. Kosongkan jika tidak ada autentikasi |
| `waha.session` | Tidak | Nama session WAHA (default: `"default"`) |
| `waha.alert_phone` | Tidak | Nomor tujuan alert WhatsApp (format: `628xxxxxxxxxx`) |
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
6. Jika gagal berturut-turut melebihi threshold → kirim alert WhatsApp

### `export` — Export data ke CSV

Mengexport log kehadiran dari database lokal ke file CSV dalam rentang tanggal tertentu.

```bash
uv run agent.py export \
  --machine "Mesin Lantai 1" \
  --from 2025-01-01 \
  --to 2025-01-31 \
  --out laporan_januari.csv
```

Output CSV memiliki kolom: `finger_id`, `punch_time`, `status`.

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

### Opsi A — Download dari GitHub Actions (paling gampang)

Repo ini punya workflow yang otomatis build `.exe` di runner Windows tiap
push ke `main` (lihat `.github/workflows/build-windows.yml`). Untuk ambil
hasilnya:

1. Buka tab **Actions** di repo GitHub → pilih run terbaru dari workflow
   "Build Windows executable".
2. Download artifact **`attendance-agent-windows`** → berisi
   `attendance-agent.exe`.

Atau jalankan manual lewat tab Actions → pilih workflow → **Run workflow**.

### Opsi B — Build manual di mesin Windows

PyInstaller tidak bisa cross-compile, jadi build harus dilakukan di mesin
Windows (bukan dari Mac/Linux).

Prasyarat: Python 3.14 + [uv](https://docs.astral.sh/uv/) terinstall.

```powershell
uv sync --group dev
uv run pyinstaller --onefile --name attendance-agent agent.py
```

Hasilnya ada di `dist\attendance-agent.exe`.

> Kalau versi PyInstaller yang ter-install belum support Python 3.14 (rilis
> masih baru), build pakai Python 3.13 di venv Windows sebagai fallback.

### Cara pakai `.exe`

Taruh `attendance-agent.exe` satu folder dengan `config.json` (path
`config.json`/`db_path` di-resolve relatif ke working directory, sama
seperti versi `uv run`). Jalankan dari `cmd`/PowerShell:

```powershell
attendance-agent.exe status
attendance-agent.exe fetch
attendance-agent.exe export --machine "Mesin Lantai 1" --from 2025-01-01 --to 2025-01-31 --out laporan.csv
```

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

`config.json` berisi kredensial (`waha.api_key`) dan detail jaringan
internal (IP mesin, URL CMS) dalam bentuk plaintext — file ini **tidak**
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
┌──────────────┐     pull_logs      ┌──────────────┐
│  Mesin ZKTeco │ ────────────────→ │  agent.py    │
│              │                    │              │
└──────────────┘                    │  SQLite DB   │
                                    │              │
                         ┌──────────┴──────┐       │
                         │  Success?       │       │
                         └──┬──────────┬───┘       │
                        Yes│          │No         │
                   ┌───────▼──┐  ┌────▼──────┐    │
                   │ Upsert   │  │ record     │    │
                   │ to local │  │ fail +     │    │
                   │ store    │  │ check alert│    │
                   └────┬─────┘  └────┬──────┘    │
                        │             │            │
                   ┌────▼─────┐  ┌────▼──────┐    │
                   │ Push ke  │  │ Threshold  │    │
                   │ CMS      │  │ tercapai?  │    │
                   └────┬─────┘  └────┬──────┘    │
                        │             │Yes         │
                   mark_pushed   ┌────▼──────┐    │
                                │ Kirim WA   │    │
                                │ alert      │    │
                                └────────────┘    │
                        ┌────────▼────────────────▼──┐
                        │  Check capacity ≥ warning% │
                        └────────────────────────────┘
```

### Guard `delete`

Perintah `delete` memeriksa apakah ada log yang belum disinkronkan (`pushed_to_cms = 0`). Jika ada, operasi ditolak kecuali flag `--force` diberikan. Ini mencegah kehilangan data yang belum terbackup ke CMS.

### Alert Logic

Alert WhatsApp dikirim **sekali per insiden**, bukan setiap siklus gagal:

1. Gagal fetch berturut-turut mencapai `offline_alert_after_cycles` → kirim alert pertama
2. Selama mesin tetap offline, alert **tidak** dikirim lagi (mencegah spam)
3. Saat mesin kembali online (`record_fetch_result(ok=True)`), counter reset
4. Jika mesin offline lagi nanti, alert baru bisa dikirim (insiden baru)

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

### WA alert tidak terkirim

- Pastikan WAHA berjalan: `curl http://localhost:3001/api/status`
- Verifikasi `waha.api_url` dan `waha.alert_phone` di config benar
- Jika menggunakan API key, pastikan `waha.api_key` diisi dengan benar
- Periksa log untuk pesan error dari WAHA

### Database corrupt

- Backup database terlebih dahulu: `cp attendance.db attendance.db.bak`
- Tool ini menggunakan WAL mode untuk mengurangi risiko korupsi data
- Jika terjadi masalah, hapus file `.db-wal` dan `.db-shm` (SQLite akan rebuild otomatis)
