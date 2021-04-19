import matplotlib.pyplot as plt

"""
times = [1.00, 20.01, 34.0, 89.0, 241.2403700351715]
loss = [0, 10, 20, 40, 60]
"""

times = [1, 7, 20, 26, 43]
loss = [0, 20, 50, 100, 200]
"""

times = [1, 21, 32, 51, 70]
reorder = [0, 10, 20, 30, 40]
"""

size = 1650442
times = [size / (1000 * i) for i in times]

plt.xlabel("delay (ms)")
plt.ylabel("throughput (kBps)")
plt.plot(loss, times)
plt.show()
