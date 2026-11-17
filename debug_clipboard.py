import pyperclip

text = pyperclip.paste()
lines = text.strip().split('\n')
print(f"Total lines: {len(lines)}")
print()
for i, line in enumerate(lines):
    print(f"[{i:3d}] {repr(line.replace(chr(13), ''))}")
