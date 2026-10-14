# Inspo — reference tracks by what they teach (template)

_A listening list, not a style. Grouped by the lesson, so it is useful for any track.
**In index** = there is a verified listen card (`find_audio_refs` finds it; model-heard:
gist right, detail unreliable). **Queue** = not in the index yet: to listen, the owner
picks the focus (`tools/audio_ref.py <url> --focus …`), then it becomes a card. Sources
and quotes live in `borrowed-techniques.md`. The public repository ships this file
without the author's list._

Entry format:

```markdown
## The lesson (e.g. "Drums that arrive late, or never")
- Artist — "Track" · in index | **queue** · one line on what the track does for this lesson,
  with its evidence type (model-heard / measured / owner-confirmed / review).
- The owner's own: Track · in index · the same, marked owner-confirmed only when they said so.
```

Suggested lesson groups (keep, rename or replace):
- Drums that arrive late (or never)
- Staged strips (what leaves first, what stands last)
- Kick as the sub / kick against the wall
- Half-time, toms, ritual weight
- Machine rigidity, meter, loops
- Silence, reverse, the drop

## Card hygiene (for the owner to decide — not changed by the sidekick)
- A verified card whose `identity` names a different record is untrusted: mark it
  suspect or re-listen (`tools/audio_ref.py --mark-suspect <ref_id> "<reason>"`).
- A working bounce of a piece in progress can be listened to as a card too; its DONTs
  then describe a work-in-progress state, not intent — keep such cards out of steering
  unless the owner marks them.
