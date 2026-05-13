@file:OptIn(kotlinx.serialization.ExperimentalSerializationApi::class)

package com.sixoffive.ao.jarvis.net

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonClassDiscriminator

/**
 * Wire protocol — mirrors shared/jarvis_shared/protocol.py. Stays in sync
 * with the Python client; the discriminator field is "type".
 */

const val PROTOCOL_VERSION = 1

@Serializable
@JsonClassDiscriminator("type")
sealed interface Outgoing  // client -> server

@Serializable
@SerialName("hello")
data class Hello(
    val version: Int = PROTOCOL_VERSION,
    @SerialName("client_id") val clientId: String,
    val hostname: String,
) : Outgoing

@Serializable
@SerialName("command")
data class Command(val text: String) : Outgoing

@Serializable
@SerialName("ping")
class Ping : Outgoing

@Serializable
@JsonClassDiscriminator("type")
sealed interface Incoming  // server -> client

@Serializable
@SerialName("welcome")
data class Welcome(
    @SerialName("session_id") val sessionId: String,
    val version: Int = PROTOCOL_VERSION,
) : Incoming

@Serializable
@SerialName("transcript")
data class Transcript(val text: String, val final: Boolean = false) : Incoming

@Serializable
@SerialName("thinking")
data class Thinking(val note: String = "") : Incoming

@Serializable
@SerialName("say")
data class Say(
    val text: String,
    @SerialName("audio_url") val audioUrl: String? = null,
) : Incoming

@Serializable
@SerialName("error")
data class ErrorMsg(val code: String, val message: String) : Incoming

@Serializable
@SerialName("pong")
class Pong : Incoming

val protocolJson: Json = Json {
    ignoreUnknownKeys = true
    classDiscriminator = "type"
    encodeDefaults = true
}
