"""The itinerary vocabulary every part of the agent layer shares."""

SLOTS = ("morning", "afternoon", "evening")

# The builder's only activity that is not a place: free time. It is never
# checked against the attraction list, never pinned on the map, and never
# counted as a top activity.
FREE_TIME = "Explore the area"
