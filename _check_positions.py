import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from real_monitor import get_real_positions
positions, err = get_real_positions()
if err:
    print(f'Error: {err}')
elif positions:
    for coin, pos in positions.items():
        side = pos['side']
        size = pos['size']
        entry = pos['entry']
        upl = pos.get('upl', 0)
        print(f'{coin}: {side} {size}张 @{entry:.4f} 浮盈${float(upl):.2f}')
else:
    print('No positions')
