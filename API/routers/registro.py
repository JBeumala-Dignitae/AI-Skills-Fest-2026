from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_

from database import get_db
from models import RegistroTiempo, EstadoRegHor, OrigenRegHor
from auth import get_current_upn

router = APIRouter()

MAX_SEGUNDOS_JORNADA = 43200  # 12 horas
TZ_LOCAL = timezone(timedelta(hours=2))  # Europa/Madrid horario de verano


# ── Schemas ──────────────────────────────────────────────────────────────────

class FicharEntradaBody(BaseModel):
    origen: OrigenRegHor = OrigenRegHor.webapp


class FicharSalidaBody(BaseModel):
    origen: OrigenRegHor = OrigenRegHor.webapp


class CorregirEntradaBody(BaseModel):
    momento_entrada: str  # 'aaaa-mm-dd hh:mm:ss'
    comentario_justificacion: str

    @field_validator("comentario_justificacion")
    @classmethod
    def comentario_requerido(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El comentario de justificación es obligatorio")
        return v.strip()

    @field_validator("momento_entrada")
    @classmethod
    def parsear_fecha(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise ValueError("El formato de momento_entrada debe ser 'aaaa-mm-dd hh:mm:ss'")
        return v


class CorregirSalidaBody(BaseModel):
    momento_salida: str  # 'aaaa-mm-dd hh:mm:ss'
    comentario_justificacion: str

    @field_validator("comentario_justificacion")
    @classmethod
    def comentario_requerido(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("El comentario de justificación es obligatorio")
        return v.strip()

    @field_validator("momento_salida")
    @classmethod
    def parsear_fecha(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise ValueError("El formato de momento_salida debe ser 'aaaa-mm-dd hh:mm:ss'")
        return v


class ConsultarHorasBody(BaseModel):
    fecha_desde: Optional[str] = None
    fecha_hasta: Optional[str] = None

    @field_validator("fecha_desde", "fecha_hasta", mode="before")
    @classmethod
    def parsear_fecha(cls, v):
        if v is None:
            return v
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("El formato de fecha debe ser 'aaaa-mm-dd'")
        return v


# ── Helpers ───────────────────────────────────────────────────────────────────

def ahora_local() -> datetime:
    return datetime.now(TZ_LOCAL)


def parsear_local(cadena: str) -> datetime:
    """Interpreta una cadena 'aaaa-mm-dd hh:mm:ss' como hora local (+02:00)."""
    return datetime.strptime(cadena, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_LOCAL)


def concatenar_comentario(existente: str | None, nuevo: str) -> str:
    if existente:
        return f"{existente} | {nuevo}"
    return nuevo


def segundos_a_hhmmss(segundos: int) -> str:
    h = segundos // 3600
    m = (segundos % 3600) // 60
    s = segundos % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


async def hay_solapamiento(
    db: AsyncSession,
    upn: str,
    entrada: datetime,
    salida: datetime,
    excluir_id: int | None = None,
) -> RegistroTiempo | None:
    """Devuelve el primer registro completo del usuario que se solape con [entrada, salida]."""
    condiciones = [
        RegistroTiempo.upn_empleado == upn,
        RegistroTiempo.estado == EstadoRegHor.completo,
        RegistroTiempo.entrada < salida,
        RegistroTiempo.salida > entrada,
    ]
    if excluir_id is not None:
        condiciones.append(RegistroTiempo.id != excluir_id)

    result = await db.execute(
        select(RegistroTiempo).where(and_(*condiciones)).limit(1)
    )
    return result.scalar_one_or_none()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/fichar-entrada")
async def fichar_entrada(
    body: FicharEntradaBody,
    upn: str = Depends(get_current_upn),
    db: AsyncSession = Depends(get_db),
):
    pendiente = await db.execute(
        select(RegistroTiempo)
        .where(
            and_(
                RegistroTiempo.upn_empleado == upn,
                RegistroTiempo.estado == EstadoRegHor.salida_pendiente,
            )
        )
        .limit(1)
    )
    if pendiente.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="Ya existe una entrada pendiente de salida. Fiche la salida antes de registrar una nueva entrada."
        )

    ahora = ahora_local()
    registro = RegistroTiempo(
        upn_empleado=upn,
        entrada=ahora,
        salida=None,
        segundos_trabajados=None,
        estado=EstadoRegHor.salida_pendiente,
        origen=body.origen,
        comentario_justificacion=None,
        upn_alta=upn,
        upn_ultima_modificacion=upn,
        fecha_alta=ahora,
        fecha_ultima_modificacion=ahora,
    )
    db.add(registro)
    await db.commit()
    await db.refresh(registro)
    return {"id": registro.id, "entrada": registro.entrada, "estado": registro.estado}


@router.post("/fichar-salida")
async def fichar_salida(
    body: FicharSalidaBody,
    upn: str = Depends(get_current_upn),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(RegistroTiempo)
        .where(
            and_(
                RegistroTiempo.upn_empleado == upn,
                RegistroTiempo.estado == EstadoRegHor.salida_pendiente,
            )
        )
        .order_by(RegistroTiempo.entrada.desc())
        .limit(1)
    )
    registro = result.scalar_one_or_none()

    if registro is None:
        raise HTTPException(status_code=404, detail="No hay ningún registro pendiente de salida")

    ahora = ahora_local()
    segundos = int((ahora - registro.entrada).total_seconds())

    if segundos > MAX_SEGUNDOS_JORNADA:
        raise HTTPException(
            status_code=400,
            detail=f"El tiempo transcurrido ({segundos}s) supera el máximo permitido (43200s / 12h)"
        )

    solapado = await hay_solapamiento(db, upn, registro.entrada, ahora, excluir_id=registro.id)
    if solapado:
        raise HTTPException(
            status_code=409,
            detail=f"La jornada se solaparía con el registro existente id={solapado.id}"
        )

    registro.salida = ahora
    registro.segundos_trabajados = segundos
    registro.estado = EstadoRegHor.completo
    registro.origen = body.origen
    registro.upn_ultima_modificacion = upn
    registro.fecha_ultima_modificacion = ahora

    await db.commit()
    await db.refresh(registro)
    return {
        "id": registro.id,
        "entrada": registro.entrada,
        "salida": registro.salida,
        "segundos_trabajados": registro.segundos_trabajados,
        "estado": registro.estado,
    }


@router.post("/corregir-entrada")
async def corregir_entrada(
    body: CorregirEntradaBody,
    upn: str = Depends(get_current_upn),
    db: AsyncSession = Depends(get_db),
):
    momento = parsear_local(body.momento_entrada)
    ahora = ahora_local()

    if momento >= ahora:
        raise HTTPException(status_code=400, detail="El momento de entrada debe ser anterior al momento actual")

    # Solapamiento: la nueva entrada no puede caer dentro de un registro completo existente
    result = await db.execute(
        select(RegistroTiempo).where(
            and_(
                RegistroTiempo.upn_empleado == upn,
                RegistroTiempo.estado == EstadoRegHor.completo,
                RegistroTiempo.entrada <= momento,
                RegistroTiempo.salida >= momento,
            )
        ).limit(1)
    )
    solapado = result.scalar_one_or_none()
    if solapado:
        raise HTTPException(
            status_code=409,
            detail=f"El momento de entrada se solapa con el registro existente id={solapado.id}"
        )

    registro = RegistroTiempo(
        upn_empleado=upn,
        entrada=momento,
        salida=None,
        segundos_trabajados=None,
        estado=EstadoRegHor.salida_pendiente,
        origen=OrigenRegHor.webapp,
        comentario_justificacion=body.comentario_justificacion,
        upn_alta=upn,
        upn_ultima_modificacion=upn,
        fecha_alta=ahora,
        fecha_ultima_modificacion=ahora,
    )
    db.add(registro)
    await db.commit()
    await db.refresh(registro)
    return {"id": registro.id, "entrada": registro.entrada, "estado": registro.estado}


@router.post("/corregir-salida")
async def corregir_salida(
    body: CorregirSalidaBody,
    upn: str = Depends(get_current_upn),
    db: AsyncSession = Depends(get_db),
):
    momento_salida = parsear_local(body.momento_salida)
    ahora = ahora_local()

    if momento_salida >= ahora:
        raise HTTPException(status_code=400, detail="El momento de salida debe ser anterior al momento actual")

    # Registro pendiente con entrada máxima anterior al momento de salida
    result = await db.execute(
        select(RegistroTiempo)
        .where(
            and_(
                RegistroTiempo.upn_empleado == upn,
                RegistroTiempo.estado == EstadoRegHor.salida_pendiente,
                RegistroTiempo.entrada < momento_salida,
            )
        )
        .order_by(RegistroTiempo.entrada.desc())
        .limit(1)
    )
    registro = result.scalar_one_or_none()

    if registro is None:
        raise HTTPException(status_code=404, detail="No hay ningún registro pendiente de salida anterior al momento indicado")

    segundos = int((momento_salida - registro.entrada).total_seconds())

    if segundos > MAX_SEGUNDOS_JORNADA:
        raise HTTPException(
            status_code=400,
            detail=f"El tiempo transcurrido ({segundos}s) supera el máximo permitido (43200s / 12h)"
        )

    solapado = await hay_solapamiento(db, upn, registro.entrada, momento_salida, excluir_id=registro.id)
    if solapado:
        raise HTTPException(
            status_code=409,
            detail=f"La jornada se solaparía con el registro existente id={solapado.id}"
        )

    registro.salida = momento_salida
    registro.segundos_trabajados = segundos
    registro.estado = EstadoRegHor.completo
    registro.comentario_justificacion = concatenar_comentario(registro.comentario_justificacion, body.comentario_justificacion)
    registro.upn_ultima_modificacion = upn
    registro.fecha_ultima_modificacion = ahora

    await db.commit()
    await db.refresh(registro)
    return {
        "id": registro.id,
        "entrada": registro.entrada,
        "salida": registro.salida,
        "segundos_trabajados": registro.segundos_trabajados,
        "estado": registro.estado,
    }


@router.get("/consultar-horas")
async def consultar_horas(
    body: ConsultarHorasBody = Depends(),
    upn: str = Depends(get_current_upn),
    db: AsyncSession = Depends(get_db),
):
    hoy = ahora_local().date()
    fecha_desde_str = body.fecha_desde or hoy.strftime("%Y-%m-%d")
    fecha_hasta_str = body.fecha_hasta or hoy.strftime("%Y-%m-%d")

    desde = datetime.strptime(fecha_desde_str, "%Y-%m-%d").replace(hour=0, minute=0, second=0, tzinfo=TZ_LOCAL)
    hasta = datetime.strptime(fecha_hasta_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=TZ_LOCAL)

    result = await db.execute(
        select(RegistroTiempo).where(
            and_(
                RegistroTiempo.upn_empleado == upn,
                or_(
                    and_(RegistroTiempo.entrada >= desde, RegistroTiempo.entrada <= hasta),
                    and_(RegistroTiempo.salida >= desde, RegistroTiempo.salida <= hasta),
                ),
            )
        ).order_by(RegistroTiempo.entrada.asc())
    )
    registros = result.scalars().all()

    total_segundos = sum(r.segundos_trabajados or 0 for r in registros)

    return {
        "registros": [
            {
                "id": r.id,
                "entrada": r.entrada,
                "salida": r.salida,
                "segundos_trabajados": r.segundos_trabajados,
                "estado": r.estado,
                "origen": r.origen,
                "comentario_justificacion": r.comentario_justificacion,
            }
            for r in registros
        ],
        "total_horas_trabajadas": segundos_a_hhmmss(total_segundos),
    }
