import numpy as np
from experiment import Experiment

console = Experiment(init_gpa=False)
console.add_flodict({
    # f'ocra40_v{ch}': 三角脉冲(0.2, 10, 100) for ch in range(9, 16)
    'ocra40_v12': (np.array([0, 100]), np.array([0, 0.5]))
})
console.run()