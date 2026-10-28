---
description: Jalankan test queue yang sudah disetujui, lalu buat report
---
Proses harus terlihat user, tapi hemat: semua test api/logic jalan dalam SATU Bash call (`run-all` mencetak satu baris per test). Jangan jalankan test di subagent, dan jangan jalankan test satu per satu kecuali mengulang test tertentu.

1. Baca `.qa/queue.json`. Kalau file tidak ada, atau `approved` bukan `true` dan user belum menyatakan setuju di chat ini: berhenti, minta persetujuan queue dulu. Kalau user sudah setuju di chat, jalankan `bash ~/.claude/scripts/qa.sh approve`.
2. Kalau ada test `api` dan `base_url` kosong: tanya user, tulis ke `queue.json` (Edit).
3. Kalau ada test mutating (api dengan method selain GET/HEAD/OPTIONS, atau ui yang submit/hapus data) dan user belum mengizinkan di chat ini: tampilkan daftarnya dan tanya sekali (AskUserQuestion). Diizinkan berarti pakai `QA_ALLOW_MUTATING=1` di langkah 4. Tidak diizinkan berarti jalankan tanpa itu (api mutating jadi `blocked`; ui mutating langsung `qa.sh set <id> blocked "tidak dikonfirmasi"`). `QA_ALLOW_PROD=1` hanya bila `base_url` mengandung "prod" DAN user mengonfirmasi eksplisit.
4. `bash ~/.claude/scripts/qa.sh run-all` (atau `QA_ALLOW_MUTATING=1 bash ~/.claude/scripts/qa.sh run-all`). Satu call, output per test sudah terlihat; jangan salin ulang.
5. Test `ui` yang tersisa (baris `UI <id>` di output): jalankan `steps` di `url` lewat skill `/browse`, ambil screenshot sebagai bukti, bandingkan dengan `expect`, lalu `bash ~/.claude/scripts/qa.sh set <id> pass|fail|blocked "<yang terlihat>"`. Halaman tidak bisa dibuka = `blocked`.
6. `bash ~/.claude/scripts/qa.sh report`, lalu tulis ringkasan singkat: total, pass, fail, blocked, dan satu baris per test `fail` (id, expected vs actual, dugaan penyebab). Kalau test `fail` berasal dari requirement `inferred`, sebut: bisa jadi asumsinya yang salah, bukan kodenya.
7. Akhiri dengan satu aksi berikutnya untuk user.
