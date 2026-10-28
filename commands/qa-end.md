---
description: Akhiri segmen QA, ekstrak goals, lalu usulkan test queue
argument-hint: [ui,api,logic] [low|medium|high]
allowed-tools: Bash(bash ~/.claude/scripts/qa.sh end:*), Agent, Write, Read(requirements.md), Read(.qa/scope.json), Skill
---
Argumen:
- Jenis test: `$1`. Isi `ui`, `api` (`request` = `api`), `logic`, dipisah koma tanpa spasi, misal `ui,logic`. Default semua (`ui,api,logic`).
- Effort: `$2`. Isi `low`, `medium`, atau `high`. Default `medium`.

1. Jalankan `bash ~/.claude/scripts/qa.sh end $1 $2`. Kalau error (argumen tidak valid), tampilkan pesan errornya dan berhenti. Kalau jumlah pesan 0, berhenti dan laporkan.
2. Panggil subagent `qa-goal-extractor` dengan prompt: path absolut `.qa/segment.txt`. JANGAN Read atau grep `.qa/segment.txt` sendiri; context utama tidak boleh melihat isinya.
3. Tulis output subagent apa adanya ke `requirements.md` di root proyek.
4. Tampilkan isi `requirements.md` ke user.
5. Load skill `qa-requirements` dan ikuti, dengan scope dari `.qa/scope.json` (jenis test + effort): rangkum pemahaman, tanya max 3 hal yang ambigu, minta konfirmasi, lalu bagi requirement jadi test queue dan tampilkan ke user. Tunggu persetujuan atau revisi. JANGAN jalankan test; itu tugas `/qa-run`.
