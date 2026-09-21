import hashlib
import io
import unittest
from PIL import Image
from app.imaging import InvalidImage, process_image


class ImagingTests(unittest.TestCase):
    def fixture(self,size=(640,480)):
        output=io.BytesIO()
        image=Image.new("RGB",size,"orange")
        exif=Image.Exif(); exif[270]="private camera comment"
        image.save(output,"JPEG",exif=exif)
        return output.getvalue()

    def test_original_integrity_and_bounded_metadata_free_variants(self):
        data=self.fixture()
        variants,phash,metadata=process_image(data)
        self.assertEqual(variants[0].sha256,hashlib.sha256(data).hexdigest())
        self.assertEqual(len(variants),5)
        self.assertEqual(len(phash),16)
        self.assertNotIn("private camera comment",str(metadata))
        for variant in variants[1:]:
            image=Image.open(io.BytesIO(variant.data))
            self.assertEqual(image.format,"WEBP")
            self.assertFalse(image.getexif())
            self.assertLessEqual(image.width,640)
            self.assertLessEqual(image.height,480)
        self.assertEqual(variants[1].width,256)

    def test_small_images_are_not_upscaled(self):
        variants,_,_=process_image(self.fixture((20,10)))
        self.assertTrue(all(v.width<=20 and v.height<=10 for v in variants))

    def test_output_is_repeatable(self):
        source=self.fixture()
        a,ha,_=process_image(source); b,hb,_=process_image(source)
        self.assertEqual(ha,hb)
        self.assertEqual([v.sha256 for v in a],[v.sha256 for v in b])

    def test_rejects_fake_and_truncated_images(self):
        for data in (b'<svg onload="alert(1)"></svg>',b'not a jpeg',self.fixture()[:50]):
            with self.assertRaises(InvalidImage): process_image(data)

    def test_rejects_pixel_bomb_before_decoding(self):
        from unittest.mock import patch
        with patch('app.imaging.MAX_PIXELS',100):
            with self.assertRaises(InvalidImage): process_image(self.fixture((20,10)))


if __name__=="__main__": unittest.main()
