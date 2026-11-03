"""Shared terminal/web Worker-title cases with explicit delivery acknowledgements."""
CASES = [
    ('answer unchanged', 'answer', [(0, 'worker.done')], 'answer', ''),
    ('fallback unchanged', 'fallback', [(0, 'worker.done')], 'fallback', ''),
    ('alert unchanged', 'alert', [(0, 'worker.failed')], 'alert', ''),
    ('reminder unchanged', 'reminder', [(0, 'worker.done')], 'reminder', ''),
    ('question stays a question', 'question', [(0, 'worker.done')], 'question', '? 提问 · {0} X'),
    ('stalled', 'notice', [(0, 'worker.stalled')], 'worker.stalled', '! 卡住 · {0} X'),
    ('grouped', 'notice', [(0, 'worker.done'), (1, 'worker.failed')], 'worker.failed',
     '! 告警 · {1} Y；✓ 完成 · {0} X'),
    ('all event priorities', 'notice', [(0, 'worker.done'), (1, 'worker.stalled'), (2, 'worker.question')],
     'worker.question', '? 提问 · {2} Z；! 卡住 · {1} Y；✓ 完成 · {0} X'),
    ('latest per task', 'notice', [(0, 'worker.failed'), (0, 'worker.question'), (0, 'worker.done'),
                                 (1, 'worker.done'), (1, 'worker.stalled')],
     'worker.stalled', '! 卡住 · {1} Y；✓ 完成 · {0} X'),
    ('global limit', 'notice', [(0, 'worker.done'), (1, 'worker.failed'), (2, 'worker.stalled'),
                              (3, 'worker.question'), (4, 'worker.done')],
     'worker.failed', '! 告警 · {1} Y；? 提问 · {3} A；! 卡住 · {2} Z 等 5 个任务'),
]


def populate(store, events):
    turn = store.open_turn()
    ids = [store.add_task(name, 'brief', 'work', None, 's', '', '')
           for name in ('X', 'Y', 'Z', 'A', 'B')[:max(e[0] for e in events) + 1]]
    for sequence, (index, kind) in enumerate(events):
        mid = store.add_message('event-%s-%s' % (turn, sequence), 'event', 'event body',
                                kind=kind, task_id=ids[index])
        store.run("UPDATE messages SET turn_id=?,status='confirmed' WHERE id=?", turn, mid)
    return turn, ids
