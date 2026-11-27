import sys
sys.path.insert(0, r"D:\ra python website")
with open('routes/dashboard.py', 'r', encoding='utf-8') as f:
    content = f.read()
    if 'UNKNOWN' in content:
        print('FOUND UNKNOWN in dashboard.py')
    if 'data_source' in content:
        for i, line in enumerate(content.split('\n'), 1):
            if 'data_source' in line.lower():
                print(f'Line {i}: {line[:120]}')
PYEOF