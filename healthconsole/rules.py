"""Every threshold in the project, and nothing else.

They live here so that adjusting one does not require reading the project.
Each "high" threshold opens a finding and each "clear" threshold closes it:
the gap between them is the hysteresis that prevents flapping.
"""

# Storage — spec §7.5
DISK_ATTENTION_PCT = 80.0
DISK_ATTENTION_CLEAR_PCT = 75.0
DISK_URGENT_PCT = 92.0
DISK_URGENT_FREE_BYTES = 3 * 1024 ** 3

# CPU temperature
CPU_TEMP_ATTENTION_C = 85.0
CPU_TEMP_ATTENTION_CLEAR_C = 80.0
CPU_TEMP_URGENT_C = 95.0
CPU_TEMP_SUSTAIN_SECONDS = 300

# CPU usage
CPU_USAGE_ATTENTION_PCT = 90.0
CPU_USAGE_ATTENTION_CLEAR_PCT = 70.0
CPU_USAGE_SUSTAIN_SECONDS = 300

# Memory
MEM_ATTENTION_AVAILABLE_PCT = 15.0
MEM_ATTENTION_CLEAR_PCT = 25.0
MEM_SWAP_ACTIVE_BYTES = 64 * 1024 ** 2

# Battery
BATTERY_WEAR_ATTENTION_PCT = 30.0
BATTERY_WEAR_URGENT_PCT = 50.0
BATTERY_LOW_PCT = 10.0

# System load, relative to the core count
LOAD_ATTENTION_RATIO = 1.5
LOAD_ATTENTION_CLEAR_RATIO = 1.0

# Score penalties — spec §7.2
PENALTY_INFO = 2
PENALTY_ATTENTION = 8
PENALTY_URGENT = 25
