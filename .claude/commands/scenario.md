Run a test scenario against the live Azure environment using the scenario runner.

Usage examples:
- /scenario A
- /scenario C --engine sk --poll
- /scenario G --engine native --poll
- /scenario --all
- /scenario --list

Arguments are passed directly to scripts/run_scenarios.py.

Available scenarios: A B C D E F G X1

Execute the following command with the provided arguments:
```
python scripts/run_scenarios.py $ARGUMENTS
```
