package com.generatecloud.app.websocket;

import com.generatecloud.app.dto.TeamEventResponse;
import java.util.function.BiConsumer;

/** Best-effort live events only. Team and image records remain the source of truth. */
interface TeamEventRelay {
    void publish(Long teamId, TeamEventResponse event);

    void subscribe(BiConsumer<Long, TeamEventResponse> consumer);
}
