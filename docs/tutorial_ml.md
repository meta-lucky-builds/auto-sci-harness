# A 10-minute start, with machine learning

You will see what the product does using a small tabular classification task. No agent, no Docker, no API key, no
spending. Install first: `docs/user_guide.md`.

## 1. Set up (1 minute)

```powershell
chh exp setup --name YOUR-NAME
```

It prints your settings and a long list of what a real session still needs. That list is for later.

## 2. Look at the screens (3 minutes)

```powershell
chh exp ui --scripted
```

Open <http://127.0.0.1:8765/>. Everything here is a scripted example, and each page says so.

1. **Today**: what needs you.
2. **Lab**: open the session. The Story grows as you watch. Press any underlined number to see what it rests on.
3. **Rehearse**: press Rehearse. A mock trial on Lab data shows every check and score. It spends nothing and proves nothing.
4. **Promote**: enter any project name, then read "what happens". You must tick a box before the button works.
5. **Case**: from Today, open "Label smoothing beats plain cross entropy". You must press to open it, because opening
   a real verdict counts as a look. Note the serif sentence: it is the verdict, in plain words.
6. **Waiting**: approvals are listed with their command. No button takes them. A person does that, in a terminal.

Press Ctrl+C in the terminal when you are done.

## 3. Try a real method on real code (3 minutes)

```powershell
cd examples\ml_starter
python make_data.py
python try_it.py
```

`make_data.py` writes `data.csv`: two measurements, a noise column and a label, where the label sets the distance from the
centre. `try_it.py` trains two models and prints their accuracy:

- `method.py`: nearest neighbours on the radius.
- `baseline.py`: always the most common label.

The method should win by a wide margin. That result is a quick look on data you can read, so it proves nothing about new
data.

## 4. See what a claim looks like (2 minutes)

Open `promote.yaml`. A claim fixes, in advance, the sentence, the method, the baseline, the measure, the smallest effect
that counts, your stated chance of being right, and the seeds. Once promoted, none of it can change.

Open `method.py`. Your code only needs `build_model(seed)` returning an object with `fit(X, y)` and `predict(X)`. The
Court computes the score, so a method cannot report its own.

## 5. What a real session adds (1 minute)

Reading only. Do not run these until `chh exp doctor` says you are ready: they need Docker and, for an agent, a key with a
spending limit.

```powershell
chh exp seal my-project --data examples\ml_starter\data.csv --label-column label --rows 400 --beacon-round R --beacon VALUE
chh exp start claude-code --project my-project
chh exp freeze SESSION --request promote.yaml
chh exp rehearse PROMOTION --rows 300
chh exp promote PROMOTION --route sealed --project my-project
```

**Seal** hides part of the data before any Lab work. **Start** runs an agent in a box that cannot reach that data.
**Freeze** fixes the code. **Rehearse** is the free mock trial. **Promote** sends it to the Court, which tries the frozen
code on the sealed part. `docs/first_session_checklist.md` has every step and every human approval.

For this kit, the freeze, rehearsal and preview steps are tested through the real code with a stand-in for Docker and no
model (`tests/test_ml_starter.py`). Nobody has run them with a real agent yet.
