---
name: qa-requirements
description: Dari requirements.md, rangkum pemahaman, klarifikasi yang ambigu, lalu bagi requirement jadi test queue (.qa/queue.json) untuk disetujui user sebelum /qa-run. Gunakan setelah /qa-end atau saat user minta test case dari requirement.
---

# qa-requirements

Input: `requirements.md` (hasil `qa-goal-extractor`) dan `.qa/scope.json` (`{"types":[...],"effort":"..."}` dari argumen `/qa-end`; tidak ada berarti `ui,api,logic` + `medium`). Output: `.qa/queue.json` yang ditampilkan ke user. Skill ini tidak menjalankan test.

## Scope
- Buat test hanya untuk jenis di `types`:
  - `api`: request HTTP ke endpoint, dicek status code + body.
  - `logic`: aturan bisnis dicek lewat command shell yang sudah ada (`go test ./pkg/x -run TestY -count=1`, `npm test -- -t "..."`). Lulus = exit 0. Kalau belum ada test code untuk logika itu, jangan masukkan ke queue; tulis di bawah tabel sebagai "butuh test code baru" dan tanya user.
  - `ui`: alur di browser (halaman, klik, isi form, teks yang tampil), dijalankan lewat `/browse` saat `/qa-run`.
- Requirement yang tidak cocok dengan jenis manapun di `types`: tulis di bawah tabel sebagai "di luar scope", jangan dibuat test.
- Effort menentukan kedalaman per requirement:
  - `low`: 1 test, perilaku utama (happy path).
  - `medium`: happy path + 1-2 negatif (validasi gagal, not found, unauthorized).
  - `high`: `medium` + edge case (kosong, batas panjang/angka, duplikat, idempotensi, hak akses) dan setiap constraint (C-) ikut diuji.

## Urutan sumber kebenaran
1. Pesan user
2. Ticket/issue
3. Deskripsi PR/MR
4. Dokumen repo (docs, README, `openapi.yaml`)
5. Kode

Sumber di atas mengalahkan sumber di bawahnya. Perilaku kode TIDAK boleh jadi expected behavior tanpa konfirmasi user: tandai `inferred` dan tampilkan sebagai asumsi.

## Langkah
1. **Klarifikasi.** Kalau ada item `low` confidence, ambigu, atau ada section "Belum jelas": tanya user, maksimal 3 pertanyaan, satu pesan. Tidak ada yang ambigu berarti jangan bertanya.
2. **Rangkuman.** 3-5 kalimat: apa yang akan diuji, batasan, asumsi `inferred`. Minta konfirmasi. Jangan buat test sebelum dikonfirmasi.
3. **Bagi jadi test.** Per requirement (G-/C-/D-) buat test sesuai `types` dan `effort` di Scope. Satu test = tepat satu `requirement_id`. Tidak ada test tanpa ID. Requirement yang tidak bisa diuji lewat HTTP: sebut di bawah tabel, jangan dipaksa jadi test.
4. **Tulis `.qa/queue.json`** (Write) dengan `approved: false`:
   ```json
   {
     "approved": false,
     "base_url": "http://localhost:8080",
     "tests": [{
       "id": "T-001", "requirement_id": "G-1", "type": "api", "inferred": false,
       "title": "Buat user dengan email valid",
       "method": "POST", "path": "/users",
       "headers": ["Authorization: Bearer $QA_TOKEN"],
       "body": {"email": "a@b.co"},
       "expect_status": 201, "expect_jq": ".id != null",
       "status": "pending", "actual": null, "evidence": null
     }, {
       "id": "T-002", "requirement_id": "C-1", "type": "logic", "inferred": false,
       "title": "Email duplikat ditolak di service",
       "cmd": "go test ./internal/user -run TestCreateDuplicateEmail -count=1",
       "cwd": "/path/ke/repo", "expect_output": "ok",
       "status": "pending", "actual": null, "evidence": null
     }, {
       "id": "T-003", "requirement_id": "G-2", "type": "ui", "inferred": false,
       "title": "Form register menampilkan error email duplikat",
       "url": "http://localhost:3000/register",
       "steps": ["isi email a@b.co", "klik Daftar"],
       "expect": "teks 'Email sudah terdaftar' tampil",
       "status": "pending", "actual": null, "evidence": null
     }]
   }
   ```
   - `base_url` (untuk `api`): dari user, `openapi.yaml`, atau README. Tidak diketahui berarti `null` (ditanya saat `/qa-run`).
   - `api`: `headers`, `body`, `expect_jq` opsional. `$QA_TOKEN` diganti dari env saat dijalankan; jangan tulis token asli.
   - `logic`: `cwd` default root proyek QA; isi path repo yang diuji. `expect_output` opsional. `cmd` hanya boleh menjalankan test/cek, bukan mengubah data atau file.
   - `ui`: `steps` berupa langkah singkat; `expect` hal yang bisa dilihat di halaman.
   - `inferred: true` bila requirement-nya `inferred`.
5. **Tampilkan ke user** sebagai tabel: `ID | Req | Type | Test | Request/Cmd/Langkah | Expected | Mutating?` (`Mutating?` = ya bila api bukan GET/HEAD/OPTIONS, atau langkah ui submit/hapus data). Di bawahnya: scope (types + effort), asumsi `inferred`, "di luar scope", "butuh test code baru", requirement yang tidak bisa diuji.
6. **Tunggu persetujuan atau revisi.** Revisi: edit `queue.json`, tampilkan ulang, `approved` tetap `false`. Setelah user setuju: `bash ~/.claude/scripts/qa.sh approve`, lalu arahkan user ke `/qa-run`.
