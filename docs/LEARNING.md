# FraudGuard AI — Learning Notes

Read this slowly. It's not a to-do list — it's the *understanding* behind the
project. When a bank's risk team questions you, the answers live here. Take your
time; nothing else needs to happen until this makes sense to you.

---

## Lesson 1 — Why fraud detection is hard

### 1.1 The problem is a needle in a haystack
Our data: **284,807 transactions, only 492 are fraud = 0.173%**, i.e. **1 fraud
for every 578 legit transactions.** This "imbalance" shapes every decision below.

### 1.2 The accuracy trap (the most important idea)
A lazy model that labels **everything "legit"** is **99.83% accurate** — and
catches **zero fraud**. So "accuracy" is a *lie* in fraud detection. Never quote
it. If someone pitches "99% accurate fraud detection," they don't understand the
problem.

### 1.3 The four outcomes (confusion matrix)
Every prediction falls in one box:

|                    | Model says FRAUD          | Model says LEGIT                        |
|--------------------|---------------------------|-----------------------------------------|
| **Actually fraud** | True Positive ✅ caught    | False Negative ❌ *fraud slips through*  |
| **Actually legit** | False Positive ⚠️ *good customer blocked* | True Negative ✅ correct  |

### 1.4 The two numbers that matter (both about money)
- **Recall** = of all real fraud, how much did we catch? Low recall → money lost.
- **False Positive Rate (FPR)** = of all good customers, how many did we wrongly
  block? High FPR → angry customers, lost sales, churn.

**The whole game is the tradeoff between these two.** Turn sensitivity up → catch
more fraud *and* block more good customers. Turn it down → happier customers *and*
more fraud slips by. There is no perfect setting — only a **dial** tuned to each
client's risk appetite. **That tunable dial is the product you're selling.**

---

## Lesson 2 — What the data actually says (from our EDA)

We ran `notebooks/01_explore.py`. Here's what each finding *means*:

### 2.1 Imbalance → we must fight it
1 fraud per 578 legit. Fix: **class weighting** during training (tell the model a
fraud mistake hurts ~578× more than a legit mistake) so it can't win by ignoring
fraud.

### 2.2 Fraud is not ONE behavior
Fraud's **mean amount is higher ($122) but its median is lower ($9.25)** than
legit. Why? Two mixed behaviors:
- **Card testing** — tiny charges to check a stolen card is live. **13.8% of
  fraud is under $1.** (drags median down)
- **Cash-out** — a few big hits once the card is confirmed. (pulls mean up)

Lesson: a single rule like "flag big amounts" misses the $0.30 tests. You need
**ML + rules together**, not one threshold.

### 2.3 Fraud comes in waves (time clustering)
Worst hour had ~35× the fraud rate of the calmest. Fraud scripts run in bursts.
*(Caveat: the hour labels are arbitrary — `Time` is just seconds since the data
started — but the clustering is real.)* This is why **velocity features** (count
of transactions per card in the last minute/hour) are gold in real systems.

### 2.4 A few features carry most of the signal
`V17, V14, V12, V10` separate fraud strongly. They're PCA-anonymized so we can't
read their meaning — but in a real bank the equivalents are things like *"new
device," "country mismatch," "odd hour for this customer."* Same math, readable
meaning.

### 2.5 Data quality
- 0 missing values ✅
- **1,081 duplicate rows** ⚠️ — must drop them *before* splitting, or the model
  sees test answers during training and its scores lie.
- Amount/Time aren't scaled — fine for tree models (XGBoost), would break
  logistic regression / neural nets.

---

## Check yourself (if you can answer these, you're ready to model)
1. Why is "99.8% accuracy" meaningless here?
2. In plain words, what's the difference between recall and false positive rate,
   and why can't you maximize both at once?
3. Why does fraud have a *lower median* but *higher mean* amount than legit?
4. Why must we drop duplicate rows before splitting the data?
5. What is a "velocity feature" and why would your PSP platform want one?

---

## A few good things to read (only these — don't drown in tabs)
- **How online payments actually work** → Stripe Docs, "Payments" section
  (stripe.com/docs/payments) — clear, vendor-neutral enough, explains auth/capture.
- **The dataset itself** → the Kaggle MLG-ULB page (linked in README) — read the
  "About" and discussion tabs.
- **Handling imbalance in ML** → the `imbalanced-learn` user guide
  (imbalanced-learn.org) — skim "Introduction" and "Over-sampling."

Read at your pace. When these five check-yourself questions feel easy, tell me and
we build the model together — one commented line at a time.
