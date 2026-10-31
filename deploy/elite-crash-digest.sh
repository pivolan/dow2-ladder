#!/bin/bash
# Дайджест крашей из пакета логов лаунчера (.txt.gz). Печатает сводку, если краши есть; иначе rc 1 (молчит).
z="$1"; [ -f "$z" ] || exit 2
T=$(zcat "$z" 2>/dev/null | tr -d '\000')
n=$(printf '%s' "$T" | grep -aoE '\([0-9]+ events\)' | grep -oE '[0-9]+' | head -1)
[ -n "$n" ] && [ "$n" -gt 0 ] || exit 1
mods=$(printf '%s' "$T" | grep -aoE 'P[14]: [A-Za-z0-9._-]+\.(exe|dll)' | sed 's/P[14]: //' | sort | uniq -c | sort -rn | awk '{printf "%s×%s ",$2,$1}')
codes=$(printf '%s' "$T" | grep -aoiE 'c0000[0-9a-f]{3}|BEX64|APPCRASH|AppHang[A-Za-z]*' | tr 'A-Z' 'a-z' | sort -u | tr '\n' ' ')
offs=$(printf '%s' "$T" | grep -aoE 'P7: 0*[0-9a-f]+' | sed 's/P7: 0*/+/' | sort -u | head -3 | tr '\n' ' ')
mm=$(printf '%s' "$T" | grep -acE 'MM-[a-z0-9]')
drops=$(printf '%s' "$T" | grep -acE 'dropped during load|Kill Dropped')
launcher=$(printf '%s' "$T" | head -1 | grep -oE '1\.[0-9.]+')
echo "⚠️ Вылеты игры в пакете: $n"
echo "модуль: ${mods:-?} · код: ${codes:-?} · смещение: ${offs:-?}"
echo "контекст: $([ "$mm" -gt 0 ] && echo 'был автоматч-матч' || echo 'автоматч не виден') · дроп-строк на загрузке: $drops · лаунчер ${launcher:-?}"
echo "нет crash-дампа (.wer/.dmp) — стека и списка сторонних DLL нет, причину не вскрыть без дампа"
