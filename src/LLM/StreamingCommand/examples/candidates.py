"""Editable profiles; saving and applying are independent actions."""
from streamingcommand import CandidateProfiles

profiles = CandidateProfiles()
print(profiles.list())
scrub = profiles.read("scrub-nurse.json")["candidates"]
# profiles.save("my-scrub", scrub, create=True)  # create / save-as
# profiles.rename("my-scrub", "renamed-scrub")
# Apply through CommandConfig(candidates_file=...) or session.configure_candidates(scrub).
