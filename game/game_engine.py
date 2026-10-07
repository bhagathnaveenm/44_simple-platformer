import math
import array
import pygame
from .player import Player
from .platform import Platform
from .hazard import Hazard

# Game Engine

WHITE = (255, 255, 255)
BROWN = (150, 100, 60)
RED = (220, 60, 60)
GREEN = (0, 200, 0)

# Maximum downward speed (pixels/frame). Keeps per-frame movement bounded
# so a fall can never be fast enough to skip over thin geometry.
MAX_FALL_SPEED = 14

# Player's default jump_strength (see player.py); difficulty scales this.
BASE_JUMP_STRENGTH = -12

# Difficulty presets. "gravity" is absolute (0.6 was the original value);
# "jump" is a multiplier on jump strength, so Medium keeps the original feel. Tune these if a level feels too easy/impossible.
DIFFICULTIES = {
    "Easy":   {"key": pygame.K_1, "gravity": 0.5, "jump": 1.05},
    "Medium": {"key": pygame.K_2, "gravity": 0.6, "jump": 1.00},
    "Hard":   {"key": pygame.K_3, "gravity": 0.7, "jump": 0.95},
}

# Input is ignored for this long after dying so the screen isn't skipped by accident.
ANY_KEY_DELAY_MS = 500

def _synth(notes, volume=0.3, wave="sine"):
    """Build a pygame Sound from a list of (start_hz, end_hz, seconds) notes.

    Sounds are generated in code so the game needs no audio asset files.
    Returns None if the mixer isn't available or uses an unsupported format.
    """
    init = pygame.mixer.get_init()
    if not init:
        return None
    freq, fmt, channels = init
    if fmt != -16:  # only signed 16-bit output is handled here
        return None

    samples = array.array("h")
    phase = 0.0
    for f0, f1, dur in notes:
        n = int(freq * dur)
        for i in range(n):
            t = i / n
            f = f0 + (f1 - f0) * t
            phase += 2 * math.pi * f / freq
            v = math.sin(phase)
            if wave == "square":
                v = 1.0 if v >= 0 else -1.0
            env = min(1.0, (1 - t) * 4, i / 100)  # quick attack, fade-out: no clicks
            val = int(32767 * volume * env * v)
            samples.extend([val] * channels)
    return pygame.mixer.Sound(buffer=samples.tobytes())


class GameEngine:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.gravity = 0.6

        self.start_x, self.start_y = 40, height - 120
        self.player = Player(self.start_x, self.start_y)

        # A simple hand-built level: platforms with gaps between them
        # (falling into a gap means falling off the bottom of the
        # screen), one hazard, and a goal near the right edge.
        ground_y = height - 40
        self.platforms = [
            Platform(0, ground_y, 160),
            Platform(220, ground_y, 140),
            Platform(420, ground_y - 60, 120),
            Platform(600, ground_y, 180),
        ]
        self.hazards = [Hazard(240, ground_y - 14, 100)]
        self.goal_x = 740

        self.score = 0
        self.font = pygame.font.SysFont("Arial", 30)
        self.title_font = pygame.font.SysFont("Arial", 64, bold=True)
        self.small_font = pygame.font.SysFont("Arial", 24)
        self.game_over = False
        self.game_over_at = 0  # time (ms) the game ended, for the input grace period

        self._init_sounds()
        self.difficulty = "Medium"
        self._apply_difficulty()

    def _init_sounds(self):
        """Create the sound effects. Fails silently if there is no audio device."""
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            self.snd_jump = _synth([(280, 620, 0.14)], 0.25, "square")
            self.snd_goal = _synth(
                [(523, 523, 0.09), (659, 659, 0.09), (784, 784, 0.09), (1047, 1047, 0.22)],
                0.3, "sine")
            self.snd_death = _synth([(420, 70, 0.55)], 0.3, "square")
        except pygame.error:
            self.snd_jump = self.snd_goal = self.snd_death = None

    def _play(self, sound):
        if sound is not None:
            sound.play()

    def _apply_difficulty(self):
        preset = DIFFICULTIES[self.difficulty]
        self.gravity = preset["gravity"]
        # Scale the Player's own (negative) jump strength; Medium = original -12.
        self.player.jump_strength = BASE_JUMP_STRENGTH * preset["jump"]

    def _end_game(self):
        """Switch to the game-over state (fall off screen or hazard hit)."""
        self.game_over = True
        self.game_over_at = pygame.time.get_ticks()
        self.player.vx = 0
        self._play(self.snd_death)

    def restart(self, difficulty=None):
        """Reset everything for a fresh run, optionally on a new difficulty."""
        if difficulty is not None:
            self.difficulty = difficulty
        self.player = Player(self.start_x, self.start_y)
        self._apply_difficulty()
        self.score = 0
        self.game_over = False

    def handle_event(self, event):
        if event.type != pygame.KEYDOWN:
            return

        if self.game_over:
            # Short grace period so a key the player was mashing when they
            # died (e.g. jump) doesn't instantly dismiss the screen.
            if pygame.time.get_ticks() - self.game_over_at < ANY_KEY_DELAY_MS:
                return
            for name, preset in DIFFICULTIES.items():
                if event.key == preset["key"]:
                    self.restart(name)
                    return
            if event.key in (pygame.K_r, pygame.K_RETURN):
                self.restart()  # replay on the current difficulty
            elif event.key in (pygame.K_ESCAPE, pygame.K_q):
                pygame.event.post(pygame.event.Event(pygame.QUIT))
            return

        if event.key in (pygame.K_SPACE, pygame.K_UP, pygame.K_w):
            if self.player.on_ground:  # jump() only works from the ground
                self.player.jump()
                self._play(self.snd_jump)

    def handle_input(self):
        keys = pygame.key.get_pressed()
        self.player.vx = 0
        if keys[pygame.K_LEFT] or keys[pygame.K_a]:
            self.player.vx = -self.player.speed
        if keys[pygame.K_RIGHT] or keys[pygame.K_d]:
            self.player.vx = self.player.speed

    def _find_landing_platform(self, prev_bottom, new_bottom):
        """Swept landing test.

        Returns the highest platform whose top surface the player's feet
        crossed (or touched) while moving from `prev_bottom` to
        `new_bottom` this frame, and which the player is horizontally
        over. Works for any fall speed because it compares the start and
        end of the move instead of relying on the rects overlapping at
        the end of it.
        """
        player_rect = self.player.rect()
        landing = None
        for platform in self.platforms:
            top = platform.y
            crossed_top = prev_bottom <= top <= new_bottom
            over_platform = (
                player_rect.right > platform.rect().left
                and player_rect.left < platform.rect().right
            )
            if crossed_top and over_platform:
                # If several were crossed in one frame, land on the
                # first one the player would hit: the highest.
                if landing is None or top < landing.y:
                    landing = platform
        return landing

    def update(self):
        if self.game_over:
            return

        # Gravity, capped at terminal velocity.
        self.player.vy = min(self.player.vy + self.gravity, MAX_FALL_SPEED)
        self.player.x = max(0, self.player.x + self.player.vx)

        # Remember where the feet were before moving so we can tell
        # whether this frame's movement crossed a platform's top surface.
        prev_bottom = self.player.y + self.player.height
        self.player.y += self.player.vy
        new_bottom = self.player.y + self.player.height

        self.player.on_ground = False

        if self.player.vy >= 0:
            # 1) Swept check: catches fast falls that skip past a platform.
            landing = self._find_landing_platform(prev_bottom, new_bottom)

            # 2) Original overlap check, kept as a fallback so existing
            #    behaviour (e.g. stepping up onto a ledge) is unchanged.
            if landing is None:
                for platform in self.platforms:
                    if self.player.rect().colliderect(platform.rect()):
                        landing = platform
                        break

            if landing is not None:
                self.player.y = landing.y - self.player.height
                self.player.vy = 0
                self.player.on_ground = True

        for hazard in self.hazards:
            if self.player.rect().colliderect(hazard.rect()):
                self._end_game()
                return

        if self.player.y > self.height:
            self._end_game()
            return

        if self.player.x >= self.goal_x:
            self.score += 1
            self._play(self.snd_goal)
            self.player.x, self.player.y = self.start_x, self.start_y
            self.player.vy = 0

    def render(self, screen):
        for platform in self.platforms:
            pygame.draw.rect(screen, BROWN, platform.rect())
        for hazard in self.hazards:
            pygame.draw.rect(screen, RED, hazard.rect())

        goal_rect = pygame.Rect(self.goal_x, 0, 6, self.height)
        pygame.draw.rect(screen, GREEN, goal_rect)

        pygame.draw.rect(screen, WHITE, self.player.rect())

        score_text = self.font.render(f"Score: {self.score}", True, WHITE)
        screen.blit(score_text, (10, 10))

        if self.game_over:
            self._render_game_over(screen)

    def _render_game_over(self, screen):
        w, h = screen.get_size()

        # Dim the frozen scene behind the message.
        overlay = pygame.Surface((w, h), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 170))
        screen.blit(overlay, (0, 0))

        ready = pygame.time.get_ticks() - self.game_over_at >= ANY_KEY_DELAY_MS
        if ready:
            menu = "  ".join(
                f"[{pygame.key.name(p['key'])}] {name}" for name, p in DIFFICULTIES.items()
            )
            prompt = "Play again - choose difficulty:"
            footer = f"{menu}"
            hint = f"Enter: replay on {self.difficulty}  |  Esc: exit"
        else:
            prompt = footer = hint = ""

        lines = [
            (self.title_font, "GAME OVER", RED, -90),
            (self.font, f"Final score: {self.score}", WHITE, -30),
            (self.small_font, prompt, WHITE, 30),
            (self.font, footer, GREEN, 70),
            (self.small_font, hint, WHITE, 115),
        ]
        for font, text, color, dy in lines:
            if not text:
                continue
            surf = font.render(text, True, color)
            screen.blit(surf, surf.get_rect(center=(w // 2, h // 2 + dy)))