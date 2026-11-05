GOAL:    a developer watching on LinkedIn gets "Magpie remembers what was on my screen and answers with the source" and visits the repo.
OUTPUT:  HyperFrames, 1920 x 1080, 30 fps
TIME:    38 s x 30 = 1140 frames; silent cut (music added if HeyGen sign-in is available)
LOOK:    ground #FBFAF7 (Magpie bg), ink #1C1B18, ink-2 #6E6A60; accent #0F766E only on: the answer chips, the cursor target, the end-card URL.
         Teal world #0B4F48 -> #0F766E (from icon.svg) for the reveal, claim and end card.
         Display: Inter 900, 112 px, -3% tracking; titles Inter 700 64 px; UI text Inter 400/700 at 1.8x real size (30-40 px).
         Medium: the real Magpie UI (rebuilt pieces use its labels, radii 14 px, borders rgba(20,20,19,.09)); cards shadow 0 24px 60px rgba(20,20,19,.12).
OBJECTS: Slack message card (real capture text), Ask panel, answer chips [1] Slack 8:47 [2] Mail 9:05 [3] Safari 10:04, source card, real Timeline screenshot, loop row "Reply to Sam about the contract", phone with real phone screenshot, Magpie icon.svg.
MOTION:  enter power3.out 0.5 s; move power2.inOut 0.6 s; exit power2.in 0.35 s; springs back.out(1.6) only on chips and icon; stagger 4 frames.
SHOTS:   s1 0-4 Slack card lands, window titles pile on top and bury it, clock 8:47 -> 11:52
         s2 4-7.5 "11:52" giant, then "What did Priya ask me?" word by word; the question shrinks into the Ask input
         s3 7.5-10.5 teal world: icon springs in, "Magpie" wipes in, "Your Mac remembers what you saw"
         s4 10.5-20.5 Ask panel: question bubble, answer streams, chips pop, cursor clicks [1] -> source card opens with Slack text
         s5 20.5-27.5 real Timeline screenshot push-in with "Your whole day, searchable"; then loop row, cursor clicks "-> Reminder", toast "Reminder added"
         s6 27.5-31.5 phone rises with the same answer on it: "On your phone, too"
         s7 31.5-38 teal: "Runs on your Mac. Private by default." then icon + Magpie + github.com/EeshanBembi/magpie holds 3 s
BANNED:  dark void ground, glow/particles, purple gradients, glass cards, serif display, everything fading up, three cards in a row, logo docked in a corner, title over dimmed screenshot, invented UI or numbers.
QA:      frames 2, 6, 9, 15, 19, 23, 26, 30, 36 s.
