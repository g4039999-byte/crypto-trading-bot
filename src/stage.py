def classify_stage(age_minutes):
    if age_minutes is None:
        return "UNKNOWN"

    if age_minutes <= 15:
        return "EARLY"

    if age_minutes <= 60:
        return "RISING"

    if age_minutes <= 240:
        return "MATURE"

    return "LATE"