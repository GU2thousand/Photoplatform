package com.generatecloud.app.pipeline;

import com.generatecloud.app.storage.StorageProperties;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyFactory;
import java.security.PrivateKey;
import java.security.interfaces.RSAPrivateKey;
import java.security.spec.PKCS8EncodedKeySpec;
import java.time.Clock;
import java.util.Base64;
import software.amazon.awssdk.services.cloudfront.CloudFrontUtilities;
import software.amazon.awssdk.services.cloudfront.model.CannedSignerRequest;

/** Signs both public and authorized private media; S3 is never an authorization fallback. */
public class CloudFrontSignedUrlService implements MediaUrlService {
    private final StorageProperties storage;
    private final String domain;
    private final String keyId;
    private final PrivateKey privateKey;
    private final int ttl;
    private final Clock clock;

    public CloudFrontSignedUrlService(StorageProperties storage, PipelineProperties pipeline) {
        this(storage, pipeline, Clock.systemUTC());
    }
    CloudFrontSignedUrlService(StorageProperties storage, PipelineProperties pipeline, Clock clock) {
        this.storage = storage;
        this.clock = clock;
        this.domain = pipeline.getCdnDomain();
        this.keyId = pipeline.getCdnKeyPairId();
        this.ttl = pipeline.getMediaUrlTtlSeconds();
        MediaDeliveryConfig.validateTtl(ttl);
        if (domain == null || domain.length() > 253
                || !domain.matches("(?i)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
                || keyId == null || !keyId.matches("[A-Za-z0-9]+"))
            throw new IllegalStateException("CloudFront requires a hostname without scheme/path and a valid public key ID");
        try {
            boolean inline = !pipeline.getCdnPrivateKeyPem().isBlank();
            boolean file = !pipeline.getCdnPrivateKeyPath().isBlank();
            if (inline == file) throw new IllegalArgumentException("Configure exactly one CloudFront private key source");
            String pem = inline ? pipeline.getCdnPrivateKeyPem() : Files.readString(Path.of(pipeline.getCdnPrivateKeyPath()));
            if (!pem.contains("-----BEGIN PRIVATE KEY-----"))
                throw new IllegalArgumentException("CloudFront signing key must be a PKCS8 RSA private key");
            byte[] der = Base64.getDecoder().decode(pem.replace("-----BEGIN PRIVATE KEY-----", "")
                    .replace("-----END PRIVATE KEY-----", "").replaceAll("\\s", ""));
            privateKey = KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(der));
            if (((RSAPrivateKey) privateKey).getModulus().bitLength() != 2048)
                throw new IllegalArgumentException("CloudFront requires an RSA 2048-bit signing key");
        } catch (Exception exception) {
            // Do not retain the exception: malformed PEM parsers can include secret input.
            throw new IllegalStateException("Invalid CloudFront signing key; supply a PKCS8 RSA 2048-bit private key via CDN_PRIVATE_KEY_PEM or CDN_PRIVATE_KEY_PATH");
        }
    }

    public String download(String key, boolean approvedPublic) {
        try {
            String resource = new URI("https", domain, "/" + storage.qualify(key), null).toASCIIString();
            var request = CannedSignerRequest.builder().resourceUrl(resource).keyPairId(keyId)
                    .privateKey(privateKey).expirationDate(clock.instant().plusSeconds(ttl)).build();
            return CloudFrontUtilities.create().getSignedUrlWithCannedPolicy(request).url();
        } catch (java.net.URISyntaxException exception) { throw new IllegalArgumentException("Invalid media object key"); }
    }
    public int ttlSeconds() { return ttl; }
}
