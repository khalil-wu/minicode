import importlib.util
import unittest
from pathlib import Path

from PIL import Image

SKILL_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = SKILL_DIR / "scripts" / "despill_chroma_edges.py"
SPEC = importlib.util.spec_from_file_location("despill_chroma_edges", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {MODULE_PATH}")
DESPILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DESPILL)


class ChromaMatteDecontaminationTest(unittest.TestCase):
    def test_does_not_change_soft_pixels_away_from_transparency_boundary(self) -> None:
        image = Image.new("RGBA", (7, 7), (40, 90, 140, 255))
        image.putpixel((3, 3), (150, 80, 180, 128))

        cleaned, _ = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
            edge_radius=2,
        )

        self.assertEqual(cleaned.getpixel((3, 3)), image.getpixel((3, 3)))

    def test_does_not_reprocess_already_clean_edge(self) -> None:
        image = Image.new("RGBA", (3, 3), (0, 0, 0, 0))
        image.putpixel((1, 1), (235, 40, 220, 128))

        cleaned, first_report = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
        )
        cleaned_again, second_report = DESPILL.decontaminate_image(
            cleaned,
            chroma_key=(255, 0, 255),
        )

        self.assertEqual(cleaned_again.tobytes(), cleaned.tobytes())
        self.assertGreater(first_report["decontaminated_pixels"], 0)
        self.assertEqual(second_report["decontaminated_pixels"], 0)

    def test_strength_zero_is_an_exact_before_preview(self) -> None:
        image = Image.new("RGBA", (3, 3), (0, 0, 0, 0))
        image.putpixel((1, 1), (170, 90, 210, 128))

        cleaned, _ = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
            strength=0,
        )

        self.assertEqual(cleaned.tobytes(), image.tobytes())

    def test_preserves_non_key_opaque_boundary_color(self) -> None:
        image = Image.new("RGBA", (9, 9), (0, 0, 0, 0))
        for y in range(2, 7):
            for x in range(2, 7):
                image.putpixel((x, y), (20, 90, 210, 255))
        for y in range(3, 6):
            for x in range(3, 6):
                image.putpixel((x, y), (10, 60, 160, 255))

        cleaned, report = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
            edge_radius=2,
            spill_tolerance=0.04,
        )

        self.assertEqual(cleaned.getpixel((2, 4)), image.getpixel((2, 4)))
        self.assertEqual(report["spill_suppressed_pixels"], 0)

    def test_preserves_nearly_neutral_opaque_boundary_color(self) -> None:
        image = Image.new("RGBA", (9, 9), (0, 0, 0, 0))
        for y in range(2, 7):
            for x in range(2, 7):
                image.putpixel((x, y), (101, 100, 101, 255))
        for y in range(3, 6):
            for x in range(3, 6):
                image.putpixel((x, y), (20, 20, 20, 255))

        cleaned, report = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
            edge_radius=2,
        )

        self.assertEqual(cleaned.getpixel((2, 4)), image.getpixel((2, 4)))
        self.assertEqual(report["spill_suppressed_pixels"], 0)

    def test_removes_dark_saturated_key_spill(self) -> None:
        image = Image.new("RGBA", (9, 9), (0, 0, 0, 0))
        for y in range(2, 7):
            for x in range(2, 7):
                image.putpixel((x, y), (50, 0, 50, 255))
        for y in range(3, 6):
            for x in range(3, 6):
                image.putpixel((x, y), (8, 8, 8, 255))

        cleaned, report = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
            edge_radius=2,
        )

        self.assertEqual(cleaned.getpixel((2, 4)), (8, 8, 8, 255))
        self.assertGreater(report["spill_suppressed_pixels"], 0)

    def test_extends_interior_color_through_non_key_translucent_edge(self) -> None:
        image = Image.new("RGBA", (7, 7), (0, 0, 0, 0))
        for y in range(1, 6):
            for x in range(1, 6):
                image.putpixel((x, y), (20, 180, 70, 80))
        for y in range(2, 5):
            for x in range(2, 5):
                image.putpixel((x, y), (30, 60, 90, 255))

        cleaned, _ = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
            edge_radius=1,
        )

        self.assertEqual(cleaned.getpixel((1, 3)), (30, 60, 90, 80))

    def test_does_not_borrow_interior_color_across_atlas_cells(self) -> None:
        image = Image.new("RGBA", (384, 208), (0, 0, 0, 0))
        image.putpixel((191, 100), (220, 40, 180, 128))
        image.putpixel((192, 100), (20, 80, 220, 255))

        cleaned, _ = DESPILL.decontaminate_image(
            image,
            chroma_key=(255, 0, 255),
            edge_radius=1,
        )

        red, green, blue, alpha = cleaned.getpixel((191, 100))
        self.assertEqual((red, green, blue), (red, red, red))
        self.assertEqual(alpha, 128)


if __name__ == "__main__":
    unittest.main()
