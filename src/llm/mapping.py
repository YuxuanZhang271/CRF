"""Translate resolved I/T/P slots into the existing CRF task vocabulary."""


def task_for(slots):
    intent, target, direction = (slots.get(k) for k in ('I', 'T', 'P'))
    if intent == 'stop':
        return 0
    if target != 'none' and intent != 'track':
        raise ValueError(f'Target {target!r} is unsupported for {intent!r}.')
    simple = {'zoom in': 3, 'zoom out': 4, 'bird view': 11, 'reset': 12,
              'clean lens': 13, 'spray water': 14, 'spray air': 15, 'track': 16}
    if intent in simple and direction == 'none':
        return simple[intent]
    directions = {'move': {'forward': 1, 'backward': 2},
                  'bend': {'up': 5, 'down': 6, 'left': 7, 'right': 8},
                  'self rotate': {'left': 9, 'right': 10}}
    if direction in directions.get(intent, {}):
        return directions[intent][direction]
    raise ValueError('The recognized intent and direction do not map to a CRF task.')
