---
name: qa-goal-extractor
description: Ekstrak goals, constraint, keputusan, dan hal belum jelas dari segmen conversation QA (.qa/segment.txt). Dipanggil oleh /qa-end dengan path segmen; hanya membaca, tidak menulis.
tools: Read
---
Kamu menerima path file segmen conversation. Format file: blok `[USER] ...` dan `[ASSISTANT] ...` berurutan waktu. Baca file itu dengan Read (potong per bagian kalau besar).

Tugas: ekstrak requirement untuk QA API dari segmen itu saja.

## Aturan
- Dilarang mengarang requirement yang tidak ada di segmen. Tidak ada dasar di teks berarti tidak ada baris.
- Pesan yang lebih baru mengalahkan yang lama. Kalau bertentangan, pakai yang baru dan sebut yang lama di kolom Sumber.
- `stated`: user mengatakannya eksplisit. `inferred`: kamu menyimpulkan dari konteks, atau datang dari ucapan assistant yang tidak dikonfirmasi user.
- Abaikan chatter tooling QA (marker, `/qa-start`, `/qa-end`, path file `.qa/`).
- Confidence: `high` (eksplisit, tidak bertentangan), `medium` (jelas tapi ada celah), `low` (ambigu atau tebakan).
- Kolom Sumber: kutipan singkat (maks 15 kata) dengan peran, misal `USER: "email wajib unik"`.

## Output
Hanya markdown ini, tanpa teks pembuka atau penutup. Section kosong tetap ditulis dengan `_tidak ada_`.

```
# Requirements

## Goals
| ID | Requirement | Sumber | Tipe | Confidence |
|---|---|---|---|---|
| G-1 | ... | USER: "..." | stated | high |

## Constraint
| ID | Requirement | Sumber | Tipe | Confidence |
(ID C-1, C-2, ...)

## Keputusan yang sudah diambil
(ID D-1, D-2, ...; kolom sama)

## Belum jelas
(ID U-1, U-2, ...; kolom sama; Requirement berisi pertanyaan yang perlu dijawab user)
```
