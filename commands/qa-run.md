---
description: Jalankan test queue yang sudah disetujui, satu per satu, lalu buat report
---
Proses harus terlihat user: satu test = satu Bash call + satu baris hasil. Jangan jalankan test di subagent.

1. Baca `.qa/queue.json`. Kalau file tidak ada, atau `approved` bukan `true` dan user belum menyatakan setuju di chat ini: berhenti, minta persetujuan queue dulu. Kalau user sudah setuju di chat, jalankan `bash ~/.claude/scripts/qa.sh approve`.
2. Kalau ada test `api` dan `base_url` kosong: tanya user, tulis ke `queue.json` (Edit).
3. Kalau ada test mutating (api dengan method selain GET/HEAD/OPTIONS, atau ui yang submit/hapus data): tampilkan daftarnya dan tanya sekali (AskUserQuestion) boleh dijalankan atau tidak. Kalau ya, awali setiap `run` api dengan `QA_ALLOW_MUTATING=1`. Kalau tidak, jalankan tanpa itu (api jadi `blocked`; ui mutating langsung `qa.sh set <id> blocked "tidak dikonfirmasi"`). `QA_ALLOW_PROD=1` hanya bila `base_url` mengandung "prod" DAN user mengonfirmasi eksplisit.
4. Loop:
   a. `bash ~/.claude/scripts/qa.sh next`. Kalau `test` null, keluar dari loop.
   b. Cetak satu baris: `▶ [n/total] <id> (<requirement_id>) <type> <method path | cmd | url>`.
   c. `api` / `logic`: `bash ~/.claude/scripts/qa.sh run <id>` dan tampilkan baris hasilnya.
   d. `ui`: jalankan `steps` di `url` lewat skill `/browse` (setiap langkah terlihat), ambil screenshot sebagai bukti, bandingkan dengan `expect`, lalu `bash ~/.claude/scripts/qa.sh set <id> pass|fail|blocked "<yang terlihat>"`. Halaman tidak bisa dibuka = `blocked`.
5. `bash ~/.claude/scripts/qa.sh report`, lalu tampilkan ringkasan: total, pass, fail, blocked, dan satu baris per test `fail` (id, expected vs actual). Kalau test `fail` berasal dari requirement `inferred`, sebut itu: bisa jadi requirement-nya yang salah, bukan kodenya.
6. Akhiri dengan satu aksi berikutnya untuk user.
