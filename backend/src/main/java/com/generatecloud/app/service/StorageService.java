package com.generatecloud.app.service;

import com.generatecloud.app.exception.BadRequestException;
import jakarta.annotation.PostConstruct;
import java.awt.Color;
import java.awt.Font;
import java.awt.GradientPaint;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Locale;
import java.util.UUID;
import javax.imageio.ImageIO;
import lombok.Getter;
import lombok.RequiredArgsConstructor;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

@Service
@RequiredArgsConstructor
public class StorageService {

    @Value("${app.storage.root}")
    private String storageRoot;

    private Path originalsDir;
    private Path thumbnailsDir;

    @PostConstruct
    void init() {
        try {
            originalsDir = Path.of(storageRoot, "originals");
            thumbnailsDir = Path.of(storageRoot, "thumbnails");
            Files.createDirectories(originalsDir);
            Files.createDirectories(thumbnailsDir);
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to initialize storage directories", exception);
        }
    }

    public StoredImage store(MultipartFile file) {
        if (file.isEmpty() || file.getContentType() == null || !file.getContentType().startsWith("image/")) {
            throw new BadRequestException("Please upload a valid image");
        }

        String extension = extension(file.getOriginalFilename());
        String storedName = UUID.randomUUID() + extension;
        String thumbnailName = UUID.randomUUID() + ".png";
        Path originalPath = originalsDir.resolve(storedName);
        Path thumbnailPath = thumbnailsDir.resolve(thumbnailName);

        try (InputStream inputStream = file.getInputStream()) {
            Files.copy(inputStream, originalPath, StandardCopyOption.REPLACE_EXISTING);
            generateThumbnail(originalPath, thumbnailPath);
            return new StoredImage(file.getOriginalFilename(), storedName, thumbnailName, file.getSize());
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to store uploaded image", exception);
        }
    }

    public StoredImage generateDemoImage(String title, Color start, Color end) {
        String storedName = UUID.randomUUID() + ".png";
        String thumbnailName = UUID.randomUUID() + ".png";
        Path originalPath = originalsDir.resolve(storedName);
        Path thumbnailPath = thumbnailsDir.resolve(thumbnailName);

        try {
            writeGeneratedImage(title, start, end, 1600, 1000, originalPath);
            generateThumbnail(originalPath, thumbnailPath);
            return new StoredImage(title + ".png", storedName, thumbnailName, Files.size(originalPath));
        } catch (IOException exception) {
            throw new IllegalStateException("Failed to create demo image", exception);
        }
    }

    public Path resolveOriginal(String storedName) {
        return originalsDir.resolve(storedName);
    }

    public Path resolveThumbnail(String thumbnailName) {
        return thumbnailsDir.resolve(thumbnailName);
    }

    private void writeGeneratedImage(String title, Color start, Color end, int width, int height, Path target)
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
        ImageIO.write(image, "png", target.toFile());
    }

    private void generateThumbnail(Path source, Path target) throws IOException {
        BufferedImage original = ImageIO.read(source.toFile());
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
        ImageIO.write(thumbnail, "png", target.toFile());
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
