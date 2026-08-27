# 60-second quickstart

Record two turns, close the journey, fold it into a trainable step.

```python
from odyssey.fold import fold
from odyssey.primitives import JourneyEvent, Message, Terminal
from odyssey.spool import Spool, SpoolConfig

spool = Spool(SpoolConfig(root=".odyssey"))


def record(seq, **payload):
    spool.record(JourneyEvent(journey_id="call_8891", seq=seq, **payload))


record(0, kind="message", message=Message(role="user", content="Book me for Tuesday at 3."))
record(1, kind="message", message=Message(role="assistant", content="Booked for 3pm."))
record(2, kind="terminal", terminal=Terminal(termination_reason="ENV_DONE"))

result = fold(spool.read("call_8891"), data_source="voice")
assert result.trainable
for step in result.journey.steps:
    if step.trainable_status == "trainable":
        train_on(step)  # your own training loop, not part of odyssey
```

`fold()` never runs without a terminal event recorded — leaving one off is
the most common way this example breaks silently: `result.trainable` stays
`False` and nothing downstream ever sees a step.

See `Spool`, `SpoolConfig`, `JourneyEvent`, `Message`, `Terminal`, and
`fold` for the full API; `Journey.steps` and `Step.trainable_status` for
what a fold produces.
