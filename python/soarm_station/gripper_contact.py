"""One jaw lock per gamepad trigger pickup. All openings are percentages."""


class GraspAttempt:
    # The encoder fallback requires an issued closing goal and sustained
    # immobility. Progress is relative to a fixed anchor, so small steps add up.
    DEMAND_GAP = 3.0
    PROGRESS = 0.5
    DWELL_S = 0.30
    MAX_SAMPLE_GAP_S = 0.10
    MAX_HOLD_BIAS = 2.0

    def __init__(self, number):
        self.number = number
        self.held_opening = None
        self.reason = ''
        self.direction_reference = None
        self.opening = False
        self.next_status_read = 0.0
        self.last_tick = None
        self.progress_at = None
        self.progress_opening = None

    def pause_observation(self):
        self.progress_at = None
        self.last_tick = None

    def update(self, now, requested, previous_goal, measured, read_overload):
        """Return a fixed hold setpoint after contact, or None while following."""
        if self.held_opening is not None:
            return self.held_opening

        if self.last_tick is None or now - self.last_tick > self.MAX_SAMPLE_GAP_S:
            self.progress_at = None
        self.last_tick = now
        if self.direction_reference is None:
            self.direction_reference = requested
        elif abs(requested - self.direction_reference) >= self.PROGRESS:
            self.opening = requested > self.direction_reference
            self.direction_reference = requested

        if self.opening:
            self.progress_at = None
            return None
        if requested <= 0.5 and previous_goal <= requested + 0.1 and measured <= 1.0:
            self.reason = 'FULLY_CLOSED'
        elif measured - max(previous_goal, requested) >= self.PROGRESS:
            if read_overload is not None and now >= self.next_status_read:
                overloaded = read_overload()
                self.next_status_read = now + (1.0 if overloaded is None else 0.1)
                if overloaded:
                    self.reason = 'SERVO_OVERLOAD'
            if measured - max(previous_goal, requested) < self.DEMAND_GAP:
                self.progress_at = None
            elif self.progress_at is None or abs(measured - self.progress_opening) >= self.PROGRESS:
                self.progress_at, self.progress_opening = now, measured
            elif not self.reason and now - self.progress_at >= self.DWELL_S:
                self.reason = 'NO_JAW_PROGRESS'
        else:
            self.progress_at = None

        if self.reason:
            # Back off excess position error while keeping a small amount of
            # already-applied squeeze. Never tighten past the last issued goal.
            self.held_opening = max(previous_goal, measured - self.MAX_HOLD_BIAS)
        return self.held_opening
