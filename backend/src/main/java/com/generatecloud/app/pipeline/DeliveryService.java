package com.generatecloud.app.pipeline;

import com.generatecloud.app.entity.ImageAsset;
import com.generatecloud.app.entity.enums.*;
import com.generatecloud.app.exception.NotFoundException;
import lombok.RequiredArgsConstructor;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
@RequiredArgsConstructor
@ConditionalOnProperty(name="app.pipeline.enabled",havingValue="true")
public class DeliveryService {
    private final JdbcTemplate jdbc;
    private final PipelineStorage storage;
    public String url(ImageAsset image,String variant) {
        var keys=jdbc.queryForList("SELECT object_key FROM media_variants WHERE media_id=? AND asset_version=? AND variant=?",
                String.class,image.getId(),image.getAssetVersion(),variant);
        if(keys.isEmpty()) throw new NotFoundException("Image variant not found");
        boolean approved=image.getVisibility()==Visibility.PUBLIC && image.getModerationStatus()==ModerationStatus.APPROVED;
        return storage.download(keys.get(0),approved);
    }
}
