package com.generatecloud.app.service;

import com.generatecloud.app.exception.BadRequestException;
import com.generatecloud.app.storage.ObjectStorage;
import com.generatecloud.app.storage.StoredObject;
import java.awt.Color;
import java.awt.Font;
import java.awt.GradientPaint;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.util.Locale;
import java.util.UUID;
import javax.imageio.ImageIO;
import lombok.Getter;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

@Service
@RequiredArgsConstructor
public class StorageService {

    private static final String THUMBNAIL_CONTENT_TYPE = "image/png";

    private final ObjectStorage objectStorage;

    public StoredImage store(MultipartFile file) {
        if (file.isEmpty() || file.getContentType() == null || !file.getContentType().startsWith("image/")) {
            throw new BadRequestException("Please upload a valid image");
        }

        try {
            byte[] originalBytes = file.getBytes();
            String storedName = UUID.randomUUID() + extension(file.getOriginalFilename());
            String thumbnailName = UUID.randomUUID() + ".png";
            byte[] thumbnailBytes = generateThumbnail(originalBytes);

            objectStorage.putObject(originalKey(storedName), originalBytes, file.getContentType());
            objectStorage.putObject(thumbnailKey(thumbnailName), thumbnailBytes, THUMBNAIL_CONTENT_TYPE);

            return new StoredImage(file.getOriginalFilename(), storedName, thumbnailName, file.getSize());
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to store uploaded image", exception);
        }
    }

    public StoredImage generateDemoImage(String title, Color start, Color end) {
        try {
            byte[] originalBytes = writeGeneratedImage(title, start, end, 1600, 1000);
            String storedName = UUID.randomUUID() + ".png";
            String thumbnailName = UUID.randomUUID() + ".png";
            byte[] thumbnailBytes = generateThumbnail(originalBytes);

            objectStorage.putObject(originalKey(storedName), originalBytes, THUMBNAIL_CONTENT_TYPE);
            objectStorage.putObject(thumbnailKey(thumbnailName), thumbnailBytes, THUMBNAIL_CONTENT_TYPE);

            return new StoredImage(title + ".png", storedName, thumbnailName, originalBytes.length);
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to create demo image", exception);
        }
    }

    public StoredObject loadOriginal(String storedName) {
        return objectStorage.getObject(originalKey(storedName));
    }

    public StoredObject loadThumbnail(String thumbnailName) {
        return objectStorage.getObject(thumbnailKey(thumbnailName));
    }

    private String originalKey(String storedName) {
        return "originals/" + storedName;
    }

    private String thumbnailKey(String thumbnailName) {
        return "thumbnails/" + thumbnailName;
    }

    private byte[] writeGeneratedImage(String title, Color start, Color end, int width, int height)
            throws IOException {
        BufferedImage image = new BufferedImage(width, height, BufferedImage.TYPE_INT_ARGB);
        Graphics2D graphics = image.createGraphics();
        graphics.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
        graphics.setPaint(new GradientPaint(0, 0, start, width, height, end));
        graphics.fillRect(0, 0, width, height);
        graphics.setColor(new Color(255, 255, 255, 210));
        graphics.fillRoundRect(80, height - 260, width - 160, 140, 28, 28);
        graphics.setColor(new Color(18, 30, 40));
        graphics.setFont(new Font("Serif", Font.BOLD, 64));
        graphics.drawString(title, 120, height - 170);
        graphics.setFont(new Font("SansSerif", Font.PLAIN, 26));
        graphics.drawString("Generate Cloud Demo Collection", 124, height - 124);
        graphics.dispose();

        ByteArrayOutputStream outputStream = new ByteArrayOutputStream();
        ImageIO.write(image, "png", outputStream);
        return outputStream.toByteArray();
    }

    private byte[] generateThumbnail(byte[] sourceBytes) throws IOException {
        BufferedImage original = ImageIO.read(new ByteArrayInputStream(sourceBytes));
        if (original == null) {
            throw new BadRequestException("Unsupported image type");
        }

        int width = 480;
        int height = Math.max(1, original.getHeight() * width / original.getWidth());
        BufferedImage thumbnail = new BufferedImage(width, height, BufferedImage.TYPE_INT_ARGB);
        Graphics2D graphics = thumbnail.createGraphics();
        graphics.setRenderingHint(RenderingHints.KEY_INTERPOLATION, RenderingHints.VALUE_INTERPOLATION_BILINEAR);
        graphics.drawImage(original, 0, 0, width, height, null);
        graphics.dispose();

        ByteArrayOutputStream outputStream = new ByteArrayOutputStream();
        ImageIO.write(thumbnail, "png", outputStream);
        return outputStream.toByteArray();
    }

    private String extension(String originalFileName) {
        if (originalFileName == null || !originalFileName.contains(".")) {
            return ".png";
        }
        String extension = originalFileName.substring(originalFileName.lastIndexOf('.')).toLowerCase(Locale.ROOT);
        return extension.length() > 8 ? ".png" : extension;
    }

    @Getter
    public static class StoredImage {
        private final String originalFileName;
        private final String storedFileName;
        private final String thumbnailFileName;
        private final long sizeBytes;

        public StoredImage(String originalFileName, String storedFileName, String thumbnailFileName, long sizeBytes) {
            this.originalFileName = originalFileName;
            this.storedFileName = storedFileName;
            this.thumbnailFileName = thumbnailFileName;
            this.sizeBytes = sizeBytes;
        }
    }
}
